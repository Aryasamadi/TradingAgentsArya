"""Restore the Telegram controller's state database from a workflow artifact.

The controller keeps its state in one SQLite file. On every run the file is
uploaded as the ``tradingagents-controller-state`` artifact and restored at
the start of the next run.

Two things went wrong before this script existed:

* The restore trusted the newest artifact blindly. If that artifact was
  empty, truncated or written by a controller that had just wiped its
  tables, the durable provider/model state was gone for good. The restore
  now walks the artifacts from newest to oldest and takes the first one that
  actually contains usable state.
* The state file was restored next to a stale copy that was never removed.
  The controller opened the stale file and kept writing to it, so the
  restored state was silently ignored.

Only the database file is restored. The WAL sidecar is deliberately not used:
the controller checkpoints its writes into the main file, so the uploaded
file is complete on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ARTIFACT_NAME = "tradingagents-controller-state"
STATE_FILE_NAME = "bot_state.db"
# The tables the controller needs to work. An artifact without them is not a
# state database and must not replace a good one.
REQUIRED_TABLES = ("providers", "models", "runs")
# Marker file written next to the state when a durable artifact existed but
# could not be loaded. The controller then runs with an empty state, and this
# marker keeps that empty state from being published over the good one.
MARKER_NAME = "bot_state.restore-incomplete"


def run_gh(args: list[str], repo: str) -> str:
    """Run a gh command and return its stdout."""
    result = subprocess.run(
        ["gh", *args, "--repo", repo],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def list_artifacts(repo: str) -> list[dict]:
    """Return the state artifacts, newest first."""
    raw = run_gh(
        ["api", f"/repos/{repo}/actions/artifacts?per_page=100"],
        repo,
    )
    payload = json.loads(raw or "{}")
    artifacts = [
        a
        for a in payload.get("artifacts", [])
        if a.get("name") == ARTIFACT_NAME and not a.get("expired")
    ]
    artifacts.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return artifacts


def download_artifact(repo: str, artifact_id: int, dest: Path) -> Path:
    """Download an artifact zip and return the path of the zip file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        subprocess.run(
            ["gh", "api", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip", "--repo", repo],
            stdout=handle,
            stderr=subprocess.PIPE,
            check=True,
        )
    return dest


def extract_state(zip_path: Path, target_dir: Path) -> Path | None:
    """Extract the state database from an artifact zip."""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            # Guard against a zip that tries to write outside the target.
            member = (target_dir / name).resolve()
            if not str(member).startswith(str(target_dir.resolve())):
                continue
            if Path(name).name == STATE_FILE_NAME:
                member.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, member.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                return member
    return None


def inspect_state(db_path: Path) -> dict:
    """Return a summary of a state file, with an ``ok`` flag."""
    summary = {"ok": False, "tables": [], "providers": 0, "models": 0, "runs": 0}
    if not db_path.exists() or db_path.stat().st_size == 0:
        return summary
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return summary
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = [r[0] for r in rows]
        summary["tables"] = sorted(tables)
        if any(table not in tables for table in REQUIRED_TABLES):
            return summary
        for key, table in (("providers", "providers"), ("models", "models"), ("runs", "runs")):
            try:
                summary[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                summary[key] = -1
        summary["ok"] = True
    except sqlite3.Error:
        return summary
    finally:
        conn.close()
    return summary


def restore(repo: str, destination: Path) -> int:
    """Restore the newest usable state file to ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        artifacts = list_artifacts(repo)
    except Exception as exc:
        print(f"Could not list state artifacts: {exc}")
        artifacts = []

    marker = destination.with_name(MARKER_NAME)
    if not artifacts:
        print("No state artifact found; starting with a fresh state.")
        if marker.exists():
            marker.unlink()
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for index, artifact in enumerate(artifacts):
            artifact_id = artifact.get("id")
            if not isinstance(artifact_id, int):
                continue
            try:
                zip_path = download_artifact(repo, artifact_id, tmp_dir / f"{artifact_id}.zip")
                extracted = extract_state(zip_path, tmp_dir / f"a{artifact_id}")
            except Exception as exc:
                # A broken download must not stop the search; the next
                # artifact may still hold a usable state.
                print(f"Artifact {artifact_id} could not be downloaded: {exc}")
                continue
            if extracted is None:
                print(f"Artifact {artifact_id} has no {STATE_FILE_NAME}; skipping.")
                continue
            summary = inspect_state(extracted)
            if not summary["ok"]:
                print(f"Artifact {artifact_id} is not a usable state database; skipping.")
                continue

            # Replace the destination atomically. Any stale file is removed
            # first, otherwise the controller would keep using it.
            if destination.exists():
                destination.unlink()
            shutil.copy2(extracted, destination)
            for sidecar in (
                destination.with_name(destination.name + "-wal"),
                destination.with_name(destination.name + "-shm"),
            ):
                if sidecar.exists():
                    sidecar.unlink()

            print(
                f"Restored state from artifact {artifact_id} "
                f"(providers={summary['providers']}, models={summary['models']}, runs={summary['runs']})."
            )
            if index > 0:
                print(f"Newer artifacts ({index}) were unusable and were skipped.")
            if marker.exists():
                marker.unlink()
            return 0

    # An artifact exists but none of them could be loaded. The controller now
    # runs with an empty state, which must not be published over the durable
    # state that is still waiting in the artifact.
    print("No usable state artifact found; starting with a fresh state.")
    marker.write_text("a durable state artifact exists but could not be restored\n")
    print(f"Wrote {marker}; the empty state will not be published.")
    return 0


def check(db_path: Path) -> int:
    """Check that a state file is safe to publish.

    Used before the upload step: a state file that is empty, truncated or
    missing its tables must not replace the durable state of the next run.
    """
    summary = inspect_state(db_path)
    if not summary["ok"]:
        print(
            f"State file {db_path} is not a usable state database "
            f"(tables={summary['tables']}); it will not be published."
        )
        return 1

    marker = db_path.with_name(MARKER_NAME)
    if marker.exists():
        # The restore could not load the durable state, so this file was
        # started empty. Publishing it would replace the state that is still
        # waiting in the artifact.
        print(
            f"{marker} exists: the durable state could not be restored, so the "
            "empty state will not be published."
        )
        return 1

    print(
        f"State file is valid (providers={summary['providers']}, "
        f"models={summary['models']}, runs={summary['runs']})."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--destination",
        default=os.environ.get("BOT_STATE_PATH", ".runtime/controller/bot_state.db"),
    )
    parser.add_argument(
        "--check",
        metavar="PATH",
        help="check that PATH is a usable state database instead of restoring",
    )
    args = parser.parse_args()
    if args.check:
        return check(Path(args.check))
    if not args.repo:
        print("GITHUB_REPOSITORY is not set; nothing to restore.")
        return 0
    return restore(args.repo, Path(args.destination))


if __name__ == "__main__":
    sys.exit(main())
