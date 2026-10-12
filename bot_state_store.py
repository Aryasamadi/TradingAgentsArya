"""Durable encrypted persistence for the Telegram controller.

The SQLite database and agent outputs are never rewritten by this module.
Only a separately encrypted byte-for-byte database snapshot is stored remotely.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


STATE_FILE = ".bot-state/bot_state.db.enc"
API_ROOT = "https://api.github.com"


class StateStoreError(RuntimeError):
    """Remote state could not be restored or saved safely."""


def resolve_state_path(configured: str | None, module_dir: str) -> str:
    """Return a stable absolute database path, independent of process CWD."""
    value = (configured or "").strip()
    if not value:
        return str(pathlib.Path(module_dir).resolve() / "bot_state.db")
    if value == ":memory:":
        return value
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(module_dir).resolve() / path
    return str(path.resolve())


def _key(owner: str, repo: str, token: str, state_key: str) -> bytes:
    seed = state_key or f"legacy:{owner}:{repo}:{token}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def encrypt_blob(data: bytes, owner: str, repo: str, token: str, state_key: str) -> bytes:
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise StateStoreError("PyNaCl is required for encrypted state storage.") from exc
    return bytes(SecretBox(_key(owner, repo, token, state_key)).encrypt(data))


def decrypt_blob(data: bytes, owner: str, repo: str, token: str, state_key: str) -> bytes:
    try:
        from nacl.secret import SecretBox
        return SecretBox(_key(owner, repo, token, state_key)).decrypt(data)
    except Exception as exc:
        raise StateStoreError(
            "Encrypted state cannot be decrypted. BOT_STATE_KEY or BOT_GITHUB_TOKEN changed; "
            "refusing to start with an empty database."
        ) from exc


def _api(
    owner: str,
    repo: str,
    token: str,
    path: str,
    branch: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not token:
        raise StateStoreError("BOT_GITHUB_TOKEN is required for durable state storage.")
    encoded_path = urllib.parse.quote(path, safe="/")
    query = urllib.parse.urlencode({"ref": branch}) if method == "GET" else ""
    url = f"{API_ROOT}/repos/{owner}/{repo}/contents/{encoded_path}"
    if query:
        url += "?" + query
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TradingAgentsArya-state-store",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            text = response.read().decode("utf-8", "replace")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code == 404:
            return None
        raise StateStoreError(f"GitHub state API failed ({exc.code}): {detail}") from exc
    except Exception as exc:
        raise StateStoreError(f"GitHub state API unavailable: {type(exc).__name__}") from exc


def restore_state_if_missing(
    path: str,
    owner: str,
    repo: str,
    token: str,
    state_key: str,
    branch: str = "bot-state",
    fallback_branch: str = "main",
) -> bool:
    """Restore a missing/empty SQLite file; never overwrite a non-empty local DB."""
    local = pathlib.Path(path)
    if path == ":memory:":
        return False
    if local.exists() and local.stat().st_size > 0:
        return False
    if not token:
        return False

    content = None
    for ref in dict.fromkeys((branch, fallback_branch)):
        try:
            content = _api(owner, repo, token, STATE_FILE, ref)
        except StateStoreError:
            # A missing local database plus unavailable remote state is unsafe:
            # callers must not silently initialize an empty replacement.
            raise
        if content:
            break
    if not content:
        return False

    try:
        encrypted = base64.b64decode(str(content.get("content") or "").encode("ascii"), validate=False)
        if not encrypted:
            raise StateStoreError("Remote encrypted state file is empty.")
        plain = decrypt_blob(encrypted, owner, repo, token, state_key)
        if len(plain) < 100:
            raise StateStoreError("Remote state is unexpectedly small; refusing to restore it.")
        local.parent.mkdir(parents=True, exist_ok=True)
        temp = local.with_name(local.name + ".restore-tmp")
        temp.write_bytes(plain)
        try:
            check = sqlite3.connect(f"file:{temp}?mode=ro", uri=True, timeout=5)
            try:
                result = check.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise StateStoreError("Remote database failed SQLite integrity check.")
            finally:
                check.close()
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        os.replace(temp, local)
        return True
    except StateStoreError:
        raise
    except Exception as exc:
        raise StateStoreError("Remote encrypted database is invalid; local state was not replaced.") from exc


def upload_state_snapshot(
    snapshot: bytes,
    owner: str,
    repo: str,
    token: str,
    state_key: str,
    branch: str = "bot-state",
) -> None:
    """Commit an encrypted snapshot to a dedicated branch, preserving source files."""
    if not snapshot:
        raise StateStoreError("Refusing to save an empty database snapshot.")
    if len(snapshot) > 700_000:
        raise StateStoreError("State database is too large for GitHub Contents API backup.")
    encrypted = encrypt_blob(snapshot, owner, repo, token, state_key)
    existing = _api(owner, repo, token, STATE_FILE, branch)
    payload: dict[str, Any] = {
        "message": "chore(bot-state): update encrypted runtime snapshot",
        "content": base64.b64encode(encrypted).decode("ascii"),
        "branch": branch,
    }
    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]
    _api(owner, repo, token, STATE_FILE, branch, method="PUT", payload=payload)
