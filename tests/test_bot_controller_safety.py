from __future__ import annotations

import base64
import os
import sqlite3
from unittest.mock import patch

import pytest

from bot_reports import make_summary_text
from bot_state_store import (
    StateStoreError,
    decrypt_blob,
    encrypt_blob,
    resolve_state_path,
    restore_state_if_missing,
)


def test_default_database_path_is_independent_of_working_directory(tmp_path):
    path = resolve_state_path(None, str(tmp_path))
    assert path == str(tmp_path / "bot_state.db")
    assert os.path.isabs(path)


def test_relative_database_path_is_resolved_against_module_dir(tmp_path):
    assert resolve_state_path("data/bot.db", str(tmp_path)) == str(tmp_path / "data" / "bot.db")


def test_encrypted_blob_roundtrip_and_key_mismatch():
    payload = b"database-snapshot-preserve-bytes\x00\xff"
    encrypted = encrypt_blob(payload, "owner", "repo", "token", "stable-key")
    assert decrypt_blob(encrypted, "owner", "repo", "token", "stable-key") == payload
    with pytest.raises(StateStoreError):
        decrypt_blob(encrypted, "owner", "repo", "token", "wrong-key")


def test_missing_remote_token_does_not_touch_local_file(tmp_path):
    target = str(tmp_path / "missing.db")
    assert restore_state_if_missing(target, "owner", "repo", "", "", branch="bot-state") is False
    assert not os.path.exists(target)



def test_empty_sqlite_shell_restores_encrypted_remote_snapshot(tmp_path):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE models (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO models(id) VALUES('saved-model')")
    connection.commit()
    connection.close()
    snapshot = source.read_bytes()

    local = tmp_path / "bot_state.db"
    connection = sqlite3.connect(local)
    connection.execute("CREATE TABLE models (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    assert local.stat().st_size > 0

    encrypted = encrypt_blob(snapshot, "owner", "repo", "token", "stable-key")
    payload = {"content": base64.b64encode(encrypted).decode("ascii")}
    with patch("bot_state_store._api", return_value=payload):
        restored = restore_state_if_missing(
            str(local), "owner", "repo", "token", "stable-key", branch="bot-state"
        )

    assert restored is True
    assert local.read_bytes() == snapshot


def test_telegram_summary_preserves_numeric_values_and_does_not_fallback_to_pdf_slice():
    row = {
        "mode": "analysis",
        "payload_json": '{"ticker":"NVDA"}',
        "provider_name_snapshot": "Custom Provider",
        "provider_name": "",
        "model_name_snapshot": "model-x",
        "model_name": "",
    }
    info = {
        "entry": "1,234.50",
        "exit": "-0.005",
        "amount": "1 000.25 shares",
        "value": "$12,345.67",
        "summary": "THIS SHOULD NOT BE SHOWN AS A TRUNCATED FALLBACK",
        "telegram_summary": "The evidence is mixed, so the conclusion remains cautious.",
    }
    text = make_summary_text(row, info)
    for value in ("1,234.50", "-0.005", "1 000.25 shares", "$12,345.67"):
        assert value in text
    assert "THIS SHOULD NOT BE SHOWN" not in text
    assert "Custom Provider" in text
    assert "model-x" in text
