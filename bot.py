#!/usr/bin/env python3
# ruff: noqa
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller - PDF report build."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Any, Tuple, Optional

from bot_ui_config import (
    DATA_VENDOR_OPTIONS, SETTING_CHOICES, apply_setting, normalize_run_settings,
    set_data_vendor, validate_portfolio,
)

BUILD = "v20-engine-ui"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("BOT_GITHUB_TOKEN", "").strip()
STATE_KEY = os.getenv("BOT_STATE_KEY", "").strip()
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"

ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_IDS = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH = f"https://api.github.com/repos/{OWNER}/{REPO}"
WORKFLOW = "tradingagents.yml"

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_LABEL = {
    "market": "📊 Market",
    "social": "💬 Sentiment",
    "news": "📰 News",
    "fundamentals": "💰 Fundamentals",
}

TERMINAL = ("success", "failure", "cancelled")
# Every non-terminal status GitHub reports for a workflow run. A run that waits
# behind a concurrency group is reported as "pending", a run without a free
# runner as "waiting" — asking only for "queued" and "in_progress" hid them.
ACTIVE_STATUSES = ("queued", "pending", "waiting", "requested", "in_progress")
QUEUE_STATUSES = ("queued", "pending")
RUN_STATUS_LABEL = {
    "queued": "در صف",
    "pending": "منتظر نوبت",
    "waiting": "منتظر رانر",
    "requested": "درخواست شده",
    "in_progress": "در حال اجرا",
    # A cancel that GitHub accepted but has not finished yet. It is not
    # terminal, so the next sync replaces it with the real outcome.
    "cancelling": "در حال توقف",
    "success": "موفق",
    "failure": "ناموفق",
    "cancelled": "لغو شده",
}
KNOWN_SUFFIXES = ("/chat/completions", "/completions", "/messages", "/generateContent")

# The state database is uploaded as a single file, so it must always be
# complete on its own. WAL keeps the newest writes in a sidecar next to the
# file; that sidecar is wiped together with the runner, so the log is folded
# into the main file on every write (see checkpoint_state).
with contextlib.suppress(OSError):
    os.makedirs(os.path.dirname(os.path.abspath(STATE_PATH)), exist_ok=True)
DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.execute("PRAGMA busy_timeout=10000")
with contextlib.suppress(sqlite3.OperationalError):
    DB.execute("PRAGMA journal_mode=WAL")
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ui (
        chat_id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS providers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider_type TEXT NOT NULL,
        base_url TEXT NOT NULL,
        token_ciphertext BLOB,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        base_url TEXT NOT NULL,
        token_ciphertext BLOB,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        request_id TEXT PRIMARY KEY,
        workflow_run_id INTEGER,
        chat_id INTEGER NOT NULL,
        mode TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        model_id TEXT NOT NULL,
        status TEXT NOT NULL,
        conclusion TEXT NOT NULL DEFAULT '',
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
)

for stmt in (
    "ALTER TABLE runs ADD COLUMN workflow_run_id INTEGER",
    "ALTER TABLE runs ADD COLUMN conclusion TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE models ADD COLUMN provider_id TEXT",
):
    with contextlib.suppress(sqlite3.OperationalError):
        DB.execute(stmt)

DB.commit()
# An RLock, not a Lock: checkpoint_state() runs inside db() while the lock is
# already held, and a plain Lock would deadlock on that second acquire.
DB_LOCK = threading.RLock()

CACHE: dict[str, Any] = {"runs": [], "ts": 0.0}
WIZARDS: dict[int, dict[str, Any]] = {}
FLOWS: dict[int, dict[str, Any]] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def q(sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
    with DB_LOCK:
        try:
            return DB.execute(sql, args).fetchall()
        except Exception as e:
            sys.stderr.write(f"DB Query Error: {e}\nSQL: {sql}\nArgs: {args}\n")
            return []


def q1(sql: str, args: tuple | list = ()) -> Optional[sqlite3.Row]:
    with DB_LOCK:
        try:
            return DB.execute(sql, args).fetchone()
        except Exception as e:
            sys.stderr.write(f"DB Query1 Error: {e}\nSQL: {sql}\nArgs: {args}\n")
            return None


def db(sql: str, args: tuple | list = ()):
    with DB_LOCK:
        try:
            cur = DB.execute(sql, args)
            DB.commit()
            # Fold the write-ahead log into the main file right away, so the
            # file that is uploaded is complete even if the runner dies.
            checkpoint_state()
            return cur
        except Exception as e:
            sys.stderr.write(f"DB Exec Error: {e}\nSQL: {sql}\nArgs: {args}\n")
            return None


def checkpoint_state() -> None:
    """Fold the write-ahead log into the main state file.

    The controller state is uploaded as a single file, but SQLite keeps the
    newest writes in a ``-wal`` sidecar next to it. A runner that is wiped
    takes that sidecar with it, so the writes that were never folded in are
    lost and the state of the next runner looks frozen. Checkpointing on
    every write keeps the uploaded file complete on its own.
    """
    with DB_LOCK:
        try:
            # fetchall() matters: an unfinished PRAGMA statement keeps the
            # transaction open and the next commit fails with "SQL statements
            # in progress".
            DB.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except Exception as e:
            sys.stderr.write(f"DB checkpoint error: {e}\n")


def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return str(row[0]) if row else default


def set_setting(key: str, value: str) -> None:
    db(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def authorized(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in ALLOWED_IDS


def admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq is not None:
            newreq.remove_header("Authorization")
        return newreq


_HTTP_OPENER = urllib.request.build_opener(_StripAuthRedirect())


def http_json(url: str, method: str = "GET", data: Any = None, headers: dict | None = None, timeout: int = 30) -> Tuple[int, Any, str]:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req_headers = {"Accept": "application/json", "User-Agent": "TradingAgentsArya-Bot"}
    if headers:
        req_headers.update(headers)
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            parsed = None
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(raw)
            return resp.status, parsed, raw
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        with contextlib.suppress(Exception):
            body_bytes = exc.read()
        return exc.code, None, body_bytes.decode("utf-8", "replace") or f"HTTP {exc.code}"
    except Exception as exc:
        return 0, None, str(exc)


def gh(method: str, path: str, data: Any = None, timeout: int = 30):
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
    }

    status, obj, raw = http_json(GH + path, method, data, headers, timeout=timeout)
    if status >= 300 or status == 0:
        raise RuntimeError(f"GitHub API error {status}: {raw[:200]}")
    return obj


def gh_bytes(path: str) -> bytes:
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")

    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TradingAgentsArya-Bot",
    }
    req = urllib.request.Request(GH + path, headers=headers, method="GET")
    try:
        with _HTTP_OPENER.open(req, timeout=180) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc


def tg(method: str, payload: dict | None = None):
    status, obj, raw = http_json(f"{TG}/{method}", "POST", payload or {}, timeout=65)
    if status != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed: {raw[:200]}")
    return obj.get("result")


def tg_document(chat_id: int, filename: str, data: bytes, caption: str) -> None:
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    buf.write(("--" + boundary + "\r\n").encode())
    buf.write(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n' + str(chat_id).encode() + b"\r\n")

    buf.write(("--" + boundary + "\r\n").encode())
    buf.write(b'Content-Disposition: form-data; name="caption"\r\n\r\n' + caption[:1024].encode("utf-8") + b"\r\n")

    buf.write(("--" + boundary + "\r\n").encode())
    buf.write(('Content-Disposition: form-data; name="document"; filename="' + filename + '"\r\n').encode())
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(data)
    buf.write(("\r\n--" + boundary + "--\r\n").encode())

    req = urllib.request.Request(
        f"{TG}/sendDocument",
        data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        resp.read()


def answer_callback(query_id: str, text: str = "", alert: bool = False) -> None:
    with contextlib.suppress(Exception):
        tg("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})


def inline(rows: list[list[tuple[str, str]]]) -> list[list[dict[str, str]]]:
    out = []
    for row in rows:
        line = []
        for text, data in row:
            if data.startswith("url:"):
                line.append({"text": text, "url": data[4:]})
            else:
                line.append({"text": text, "callback_data": data})
        out.append(line)
    return out


def send_message(chat_id: int, text: str, rows: list[list[tuple[str, str]]] | None = None) -> int:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if rows is not None:
        payload["reply_markup"] = {"inline_keyboard": inline(rows)}
    return int(tg("sendMessage", payload)["message_id"])


def edit_message(chat_id: int, message_id: int, text: str, rows: list[list[tuple[str, str]]]) -> bool:
    try:
        tg(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": inline(rows)},
            },
        )
        return True
    except Exception as exc:
        return "message is not modified" in str(exc).lower()


def remember_ui(chat_id: int, message_id: int) -> None:
    db(
        "INSERT INTO ui(chat_id,message_id,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id,updated_at=excluded.updated_at",
        (chat_id, message_id, now()),
    )


def ui_message(chat_id: int) -> int:
    row = q1("SELECT message_id FROM ui WHERE chat_id=?", (chat_id,))
    return int(row[0]) if row else 0


def show(chat_id: int, text: str, rows: list[list[tuple[str, str]]], target_mid: int | None = None) -> int:
    mid = target_mid or ui_message(chat_id)
    if mid and edit_message(chat_id, mid, text, rows):
        remember_ui(chat_id, mid)
        return mid

    new_mid = send_message(chat_id, text, rows)
    remember_ui(chat_id, new_mid)
    return new_mid


def clean_base_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("Base URL باید با http:// یا https:// شروع شود.")
    value = value.rstrip("/")
    if not value:
        raise ValueError("Base URL معتبر نیست.")
    return value


def strip_known_suffix(url: str) -> str:
    for suffix in KNOWN_SUFFIXES:
        if url.lower().endswith(suffix.lower()):
            return url[: -len(suffix)].rstrip("/")
    return url


def chat_endpoint(base_url: str) -> str:
    low = base_url.lower()
    for suffix in KNOWN_SUFFIXES:
        if low.endswith(suffix):
            return base_url
    return base_url + "/chat/completions"


def infer_provider_name(base_url: str) -> str:
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    mapping = {
        "api.openai.com": "OpenAI",
        "api.anthropic.com": "Anthropic",
        "generativelanguage.googleapis.com": "Google",
        "api.x.ai": "xAI",
        "api.deepseek.com": "DeepSeek",
        "openrouter.ai": "OpenRouter",
        "integrate.api.nvidia.com": "Nvidia",
        "vyceai.com": "VyceAI",
    }
    for needle, name in mapping.items():
        if needle in host:
            return name
    return host.split(".")[0].capitalize() if host else "Custom"


def infer_provider_type(base_url: str) -> str:
    host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    mapping = [
        ("api.openai.com", "openai"),
        ("api.anthropic.com", "anthropic"),
        ("generativelanguage.googleapis.com", "google"),
        ("api.x.ai", "xai"),
        ("api.deepseek.com", "deepseek"),
        ("dashscope-intl.aliyuncs.com", "qwen"),
        ("dashscope.aliyuncs.com", "qwen-cn"),
        ("api.z.ai", "glm"),
        ("open.bigmodel.cn", "glm-cn"),
        ("api.minimax.io", "minimax"),
        ("api.minimaxi.com", "minimax-cn"),
        ("openrouter.ai", "openrouter"),
        ("api.mistral.ai", "mistral"),
        ("api.moonshot.ai", "kimi"),
        ("api.groq.com", "groq"),
        ("integrate.api.nvidia.com", "nvidia"),
        ("api.perplexity.ai", "perplexity"),
        ("api.together.xyz", "together"),
        ("api.fireworks.ai", "fireworks"),
        ("api.cerebras.ai", "cerebras"),
        ("api.sambanova.ai", "sambanova"),
        ("api.deepinfra.com", "deepinfra"),
        ("api.cohere.ai", "cohere"),
    ]

    for needle, provider in mapping:
        if needle == host or host.endswith("." + needle):
            return provider

    if "openai.azure.com" in host or host.endswith("cognitiveservices.azure.com"):
        return "azure"

    if "bedrock-runtime" in host and host.endswith("amazonaws.com"):
        return "bedrock"

    if host in {"localhost", "127.0.0.1"} or host.endswith(":11434"):
        return "ollama"

    return "openai_compatible"


PROVIDER_SECRET = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY",
    "deepinfra": "DEEPINFRA_API_KEY",
    "cohere": "COHERE_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "ollama": "",
}


def crypt_key() -> bytes:
    seed = STATE_KEY or f"legacy:{OWNER}:{REPO}:{GH_TOKEN}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def encrypt_token(token: str) -> bytes:
    from nacl.secret import SecretBox

    return bytes(SecretBox(crypt_key()).encrypt(token.encode("utf-8")))


def decrypt_token(ciphertext) -> str:
    if not ciphertext:
        return ""
    from nacl.secret import SecretBox

    try:
        return SecretBox(crypt_key()).decrypt(bytes(ciphertext)).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("کلید رمزگشایی تغییر کرده؛ مدل را دوباره اضافه کن.") from exc


def encrypt_request_config(obj: dict[str, Any]) -> str:
    from nacl.secret import SecretBox

    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(bytes(SecretBox(crypt_key()).encrypt(payload))).decode("ascii")


def save_provider(name: str, provider_type: str, base_url: str, token: str) -> str:
    prov_id = "p_" + uuid.uuid4().hex[:12]
    db(
        "INSERT INTO providers(id,name,provider_type,base_url,token_ciphertext,created_at) VALUES(?,?,?,?,?,?)",
        (prov_id, name, provider_type, base_url, encrypt_token(token), now()),
    )
    return prov_id


def get_provider(prov_id: str):
    return q1("SELECT * FROM providers WHERE id=?", (prov_id,))


def delete_provider(prov_id: str) -> None:
    db("DELETE FROM models WHERE provider_id=?", (prov_id,))
    db("DELETE FROM providers WHERE id=?", (prov_id,))


def model_rows():
    return q("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE")


def active_model_id() -> str:
    return get_setting("active_model", "")


def set_active_model(model_id: str) -> None:
    set_setting("active_model", model_id)


def tier_model_id(tier: str) -> str:
    if tier not in {"quick", "deep"}:
        raise ValueError("ناحیه مدل نامعتبر است.")
    return get_setting(f"{tier}_model_id", "") or active_model_id()


def set_tier_model_id(tier: str, model_id: str) -> None:
    if tier not in {"quick", "deep"}:
        raise ValueError("ناحیه مدل نامعتبر است.")
    row = q1("SELECT id FROM models WHERE id=? AND enabled=1", (model_id,))
    if not row:
        raise ValueError("مدل فعال و معتبری انتخاب نشده است.")
    set_setting(f"{tier}_model_id", model_id)


def get_run_settings() -> dict[str, Any]:
    raw = get_setting("run_settings", "")
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("تنظیمات ذخیره‌شده خراب است؛ از گزینه بازنشانی استفاده کن.") from exc
    return normalize_run_settings(value)


def save_run_settings(value: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_run_settings(value)
    set_setting("run_settings", json.dumps(normalized, ensure_ascii=False))
    return normalized


def github_secret(name: str, value: str) -> None:
    from nacl import encoding, public

    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode("utf-8"))
    gh(
        "PUT",
        f"/actions/secrets/{urllib.parse.quote(name, safe='')}",
        {"encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key["key_id"]},
    )


def write_active_model_secrets(row) -> None:
    prov = get_provider(row["provider_id"])
    if not prov:
        return

    provider_type = prov["provider_type"]
    token = decrypt_token(prov["token_ciphertext"])

    if provider_type in PROVIDER_SECRET and PROVIDER_SECRET[provider_type] and token:
        github_secret(PROVIDER_SECRET[provider_type], token)

    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider_type)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", strip_known_suffix(prov["base_url"]))
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")


def add_model_from_provider(prov_id: str, model_name: str) -> str:
    prov = get_provider(prov_id)
    if not prov:
        raise ValueError("Provider یافت نشد.")

    existing = q1("SELECT id FROM models WHERE name=? AND provider_id=? AND enabled=1", (model_name, prov_id))
    if existing:
        model_id = str(existing["id"])
        row = q1("SELECT * FROM models WHERE id=?", (model_id,))
        if row:
            with contextlib.suppress(Exception):
                write_active_model_secrets(row)
        set_active_model(model_id)
        return model_id

    model_id = "m_" + uuid.uuid4().hex[:12]
    db(
        "INSERT INTO models(id,name,provider_id,base_url,token_ciphertext,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
        (model_id, model_name, prov_id, prov["base_url"], prov["token_ciphertext"], now()),
    )

    row = q1("SELECT * FROM models WHERE id=?", (model_id,))
    if row:
        with contextlib.suppress(Exception):
            write_active_model_secrets(row)

    set_active_model(model_id)
    return model_id


def run_model_test_async(chat_id: int, model_name: str, base_url: str, token: str, provider_type: str, return_to: str):
    try:
        started = time.monotonic()

        def attempt(timeout=240):
            if provider_type == "anthropic":
                endpoint = base_url if base_url.endswith("/messages") else base_url + "/v1/messages"
                status, _, raw = http_json(
                    endpoint,
                    "POST",
                    {"model": model_name, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK only."}]},
                    {"x-api-key": token, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                    timeout=timeout,
                )
            elif provider_type == "google":
                endpoint = base_url + "/models/" + urllib.parse.quote(model_name, safe="") + ":generateContent"
                endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode({"key": token})
                status, _, raw = http_json(
                    endpoint,
                    "POST",
                    {"contents": [{"parts": [{"text": "Reply OK only."}]}], "generationConfig": {"maxOutputTokens": 8}},
                    {"Content-Type": "application/json"},
                    timeout=timeout,
                )
            else:
                endpoint = chat_endpoint(base_url)
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {token}"

                status, _, raw = http_json(
                    endpoint,
                    "POST",
                    {"model": model_name, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8},
                    headers,
                    timeout=timeout,
                )

            if status < 200 or status >= 300 or status == 0:
                raise RuntimeError(raw or f"HTTP {status}")

            return time.monotonic() - started

        try:
            elapsed = attempt()
        except RuntimeError as exc:
            err_msg = str(exc).lower()
            if "524" in err_msg or "timeout" in err_msg or "timed out" in err_msg:
                time.sleep(60)
                elapsed = attempt()
            else:
                raise

        send_message(
            chat_id,
            f"<b>✅ تست موفق</b>\nمدل: <code>{esc(model_name)}</code>\nزمان پاسخ: <b>{elapsed:.1f}s</b>",
            [[("بازگشت", return_to)]],
        )
    except Exception as exc:
        raw_error = str(exc)
        hint = ""
        if "content cannot be a plain string" in raw_error.lower():
            hint = "\n💡 <b>راهنما:</b> این مدل چت‌بات نیست."
        elif "404" in raw_error:
            hint = "\n💡 <b>راهنما:</b> مدل یافت نشد."

        send_message(
            chat_id,
            f"<b>❌ تست ناموفق</b>\nمدل: <code>{esc(model_name)}</code>\n<code>{esc(raw_error[:3000])}</code>{hint}",
            [[("بازگشت", return_to)]],
        )


def fetch_models_list(base_url: str, token: str) -> list[str]:
    endpoint = base_url.rstrip("/") + "/models"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, data, raw = http_json(endpoint, "GET", headers=headers, timeout=30)
    if status >= 300 or status == 0 or not data:
        return []

    models = []
    try:
        if isinstance(data, dict) and "data" in data:
            for item in data["data"]:
                if isinstance(item, dict) and "id" in item:
                    models.append(item["id"])
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "id" in item:
                    models.append(item["id"])
                elif isinstance(item, str):
                    models.append(item)
    except Exception:
        return []

    return models


def fetch_active_runs():
    active = []
    for status in ACTIVE_STATUSES:
        try:
            data = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=50", timeout=15) or {}
            for run in data.get("workflow_runs", []):
                if run.get("event") == "repository_dispatch":
                    active.append(run)
        except Exception:
            pass

    return list({int(run["id"]): run for run in active if run.get("id")}.values())


def dispatch_run(chat_id: int, mode: str, params: dict) -> str:
    model_id = active_model_id()
    if not model_id:
        raise ValueError("اول از بخش مدل‌ها یک مدل فعال کن.")

    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "mode": mode,
        "chat_id": str(chat_id),
        "model_id": model_id,
        "params": params,
    }

    row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,))
    prov = get_provider(row["provider_id"]) if row else None

    if row and prov:
        try:
            token = decrypt_token(prov["token_ciphertext"])
            config = {
                "request_id": request_id,
                "provider_type": prov["provider_type"],
                "model_name": row["name"],
                "base_url": strip_known_suffix(prov["base_url"]),
                "provider_env_key": PROVIDER_SECRET.get(prov["provider_type"], ""),
                "api_token": token,
            }
            if STATE_KEY:
                payload["config_enc"] = encrypt_request_config(config)
        except Exception as exc:
            sys.stderr.write(f"Failed to build encrypted model config: {exc}\n")

    gh(
        "POST",
        "/dispatches",
        {"event_type": "tradingagents_run", "client_payload": payload},
    )

    db(
        "INSERT INTO runs(request_id,workflow_run_id,chat_id,mode,payload_json,model_id,status,conclusion,notified,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
        (request_id, None, chat_id, mode, json.dumps(params, ensure_ascii=False), model_id, "queued", "", now(), now()),
    )

    return request_id


def sync_run_records() -> None:
    rows = q("SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 30")
    if not rows:
        return

    try:
        data = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page=100", timeout=15) or {}
        workflow_runs = [r for r in data.get("workflow_runs", []) if r.get("event") == "repository_dispatch"]

        for row in rows:
            run = None
            for candidate in workflow_runs:
                if row["request_id"] in str(candidate.get("display_title") or ""):
                    run = candidate
                    break

            if not run:
                continue

            status = str(run.get("status") or "queued")
            conclusion = str(run.get("conclusion") or "")
            final = conclusion if status == "completed" and conclusion else status

            db(
                "UPDATE runs SET workflow_run_id=?, status=?, conclusion=?, updated_at=? WHERE request_id=?",
                (run.get("id"), final, conclusion, now(), row["request_id"]),
            )
    except Exception:
        pass


def notify_finished() -> None:
    rows = q("SELECT * FROM runs WHERE notified=0 AND status IN " + str(TERMINAL) + " LIMIT 10")

    for row in rows:
        params = json.loads(row["payload_json"] or "{}")
        subject = params.get("ticker") or params.get("tickers") or "—"
        label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"

        if row["status"] == "success":
            emoji = "✅"
            result_text = "موفق"
        elif row["status"] == "cancelled":
            emoji = "🚫"
            result_text = "لغو شد"
        else:
            emoji = "❌"
            result_text = "ناموفق"

        text = (
            f"{emoji} {label} <code>{esc(subject)}</code> تمام شد.\n"
            f"نتیجه: <b>{esc(result_text)}</b>\n"
        )

        if row["status"] != "success" and row["conclusion"]:
            text += f"دلیل: <code>{esc(row['conclusion'])}</code>\n"

        text += "برای دریافت گزارش کامل دکمه زیر را بزن."

        buttons = [[("📄 دریافت گزارش کامل", f"view_output:{row['request_id']}")]]
        if row["workflow_run_id"]:
            buttons.append([("🔗 صفحه اجرا", f"url:https://github.com/{OWNER}/{REPO}/actions/runs/{row['workflow_run_id']}")])

        try:
            send_message(int(row["chat_id"]), text, buttons)
        except Exception:
            sys.stderr.write(traceback.format_exc())

        db("UPDATE runs SET notified=1, updated_at=? WHERE request_id=?", (now(), row["request_id"]))


def worker_loop() -> None:
    while True:
        for task in (
            sync_run_records,
            lambda: CACHE.update(runs=fetch_active_runs()),
            notify_finished,
        ):
            with contextlib.suppress(Exception):
                task()

        CACHE["ts"] = time.monotonic()
        time.sleep(20)


def cancel_run(run_id: int) -> str:
    """Cancel a workflow run and report what GitHub really did.

    GitHub answers 202 when the cancellation is accepted, 409 when the run has
    already finished and 403 when the token is not allowed to cancel it. The
    old version swallowed every error and returned normally, so the bot told
    the user "لغو شد" for runs that were still running.

    The answer is asynchronous: 202 only means the request was accepted, so
    the run is read back and the caller learns whether it really stopped.
    """
    try:
        gh("POST", f"/actions/runs/{run_id}/cancel", timeout=15)
    except RuntimeError as exc:
        status = 0
        match = re.search(r"GitHub API error (\d+)", str(exc))
        if match:
            status = int(match.group(1))
        if status == 409:
            raise RuntimeError("این اجرا قبلاً تمام شده و دیگر قابل لغو نیست.") from exc
        if status == 403:
            raise RuntimeError("اجازهٔ لغو این اجرا را ندارم.") from exc
        if status == 404:
            raise RuntimeError("این اجرا در گیت‌هاب پیدا نشد.") from exc
        raise RuntimeError(f"لغو ثبت نشد: {exc}") from exc

    # 202 means "accepted", not "cancelled". Read the run back until it
    # reaches a final state, so the message matches reality.
    for _ in range(3):
        time.sleep(1)
        try:
            run = gh("GET", f"/actions/runs/{run_id}", timeout=15) or {}
        except Exception:
            continue
        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        if status == "completed":
            if conclusion == "cancelled":
                return "cancelled"
            return f"completed:{conclusion or 'unknown'}"
    return "cancelling"


def mark_run_cancelled(workflow_run_id: int | None, outcome: str) -> None:
    """Write the outcome of a cancel back into the local run row.

    A run that really stopped is marked terminal, so the list stops showing
    it. A run that is only "cancelling" keeps a non-terminal status, so the
    next sync corrects it with whatever GitHub finally reports. The row is
    matched on the workflow run id, which is unique, so an admin stopping
    another user's run updates that run's own row.
    """
    if workflow_run_id is None:
        return
    if outcome == "cancelled":
        db(
            "UPDATE runs SET status='cancelled', conclusion='cancelled_by_user', updated_at=? "
            "WHERE workflow_run_id=?",
            (now(), workflow_run_id),
        )
    else:
        db(
            "UPDATE runs SET status='cancelling', updated_at=? WHERE workflow_run_id=?",
            (now(), workflow_run_id),
        )


def hard_cancel(chat_id: int) -> str:
    WIZARDS.pop(chat_id, None)
    FLOWS.pop(chat_id, None)

    cancelled_engines = 0
    failed = 0
    errors: list[str] = []
    pending = 0

    try:
        api_runs = fetch_active_runs()
        db_rows = [
            dict(row)
            for row in q(
                "SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 50"
            )
        ]
        # Only the runs of this chat are stopped; the list is chat-scoped, so
        # one user cannot cancel the work of another.
        entries = _collect_active_runs(chat_id, api_runs, db_rows, admin(chat_id))
        for entry in entries:
            if not entry.get("cancelable"):
                continue
            workflow_run_id = entry.get("workflow_run_id")
            if not workflow_run_id:
                # GitHub has not given this run an id yet, so there is nothing
                # to cancel; it is reported instead of being faked.
                pending += 1
                continue
            try:
                outcome = cancel_run(int(workflow_run_id))
            except Exception as exc:
                failed += 1
                errors.append(f"#{entry['request_id'][:8]}: {exc}")
                continue
            mark_run_cancelled(workflow_run_id, outcome)
            if outcome == "cancelled":
                cancelled_engines += 1
            else:
                failed += 1
                errors.append(f"#{entry['request_id'][:8]}: اجرا هنوز متوقف نشده است.")
    except Exception as exc:
        errors.append(str(exc))

    # The local rows are only marked cancelled for the runs that really
    # stopped. A failed cancel keeps its row, so the next sync can correct it.
    lines = []
    if cancelled_engines:
        lines.append(f"✅ {cancelled_engines} اجرای Engine لغو شد.")
    else:
        lines.append("ℹ️ هیچ اجرای فعالی برای لغو نبود.")
    if pending:
        lines.append(f"⏳ {pending} اجرا هنوز در گیت‌هاب ثبت نشده و قابل لغو نبود.")
    if failed:
        lines.append(f"⚠️ {failed} اجرا لغو نشد.")
        lines.extend(f"• {esc(err)}" for err in errors[:5])
    return "\n".join(lines)


def make_summary_text(row: sqlite3.Row, info: dict[str, Any]) -> str:
    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"
    label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"

    lines = [f"<b>📊 گزارش {label} <code>{esc(subject)}</code></b>"]

    # A dash is the placeholder for a field the run never produced; showing it
    # as a result reads as "the analyst said —", which is not what happened.
    def value_of(key: str) -> str:
        text = str(info.get(key) or "").strip()
        return "" if text in {"", "—", "-"} else text

    rating = value_of("rating")
    if rating:
        lines.append(f"🧭 نتیجه نهایی: <b>{esc(rating)}</b>")
    action = value_of("action")
    if action:
        lines.append(f"🧾 اقدام / سفارش: <b>{esc(action)}</b>")
    entry = value_of("entry")
    if entry:
        lines.append(f"📥 نقطه ورود: <code>{esc(entry)}</code>")
    exit_price = value_of("exit")
    if exit_price:
        lines.append(f"📤 نقطه خروج: <code>{esc(exit_price)}</code>")
    target = value_of("target")
    if target:
        lines.append(f"🎯 هدف قیمتی: <code>{esc(target)}</code>")
    stop = value_of("stop")
    if stop:
        lines.append(f"🛑 حد ضرر: <code>{esc(stop)}</code>")
    confidence = value_of("confidence")
    if confidence:
        lines.append(f"📈 سطح اطمینان: <b>{esc(confidence)}</b>")
    horizon = value_of("horizon")
    if horizon:
        lines.append(f"⏳ افق زمانی: <b>{esc(horizon)}</b>")

    if not any((rating, action, entry, exit_price, target, stop, confidence, horizon)):
        lines.append("⚠️ این اجرا هیچ نتیجه‌ای تولید نکرد.")

    summary = str(info.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append(esc(summary[:1800]))

    return "\n".join(lines)[:4096]


def send_output(chat_id: int, request_id: str) -> None:
    # A run belongs to the chat that asked for it; looking it up by request id
    # alone let anyone holding the id read another chat's report.
    row = q1("SELECT * FROM runs WHERE request_id=? AND chat_id=?", (request_id, chat_id))
    if not row:
        send_message(chat_id, "❌ این اجرا پیدا نشد.", [[("بازگشت", "outputs")]])
        return

    if not row["workflow_run_id"]:
        with contextlib.suppress(Exception):
            sync_run_records()
        row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))

    run_id = row["workflow_run_id"]
    if not run_id:
        send_message(chat_id, "⏳ هنوز Run ID ثبت نشده.", [[("بازگشت", "outputs")]])
        return

    try:
        data = gh("GET", f"/actions/runs/{run_id}/artifacts") or {}
        arts = [a for a in data.get("artifacts", []) if not a.get("expired")]

        pick = None
        for prefix in ("tradingagents-results-",):
            for art in arts:
                if str(art.get("name", "")).startswith(prefix):
                    pick = art
                    break
            if pick:
                break

        if not pick:
            send_message(chat_id, "📭 هنوز Artifact خروجی ساخته نشده.", [[("بازگشت", "outputs")]])
            return

        raw = gh_bytes(f"/actions/artifacts/{pick['id']}/zip")
    except Exception as exc:
        send_message(chat_id, f"❌ دریافت خروجی ناموفق: {esc(exc)}", [[("بازگشت", "outputs")]])
        return

    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = zf.namelist()

    info = {}
    try:
        json_name = None
        for name in names:
            if os.path.basename(name).lower() == "report.json":
                json_name = name
                break
        if json_name:
            info = json.loads(zf.read(json_name).decode("utf-8", "replace"))
    except Exception:
        info = {}

    try:
        send_message(chat_id, make_summary_text(row, info), [[("🏠 خانه", "home")]])
    except Exception:
        send_message(chat_id, "📊 گزارش آماده شد.", [[("🏠 خانه", "home")]])

    pdf_name = None
    for name in names:
        if os.path.basename(name).lower() == "report.pdf":
            pdf_name = name
            break

    if not pdf_name:
        # A run that produced no report tree says so in its status; saying
        # "PDF not found" there hides the real reason, which is that the
        # analysis never finished.
        if info.get("status") == "no_report":
            send_message(
                chat_id,
                "⚠️ این اجرا گزارشی تولید نکرد؛ نتیجه‌ای برای نمایش وجود ندارد.",
                [[("🔄 بررسی دوباره", f"view_output:{request_id}"), ("🏠 خانه", "home")]],
            )
            return
        send_message(chat_id, "⚠️ فایل PDF پیدا نشد.", [[("بازگشت", "outputs")]])
        return

    pdf_bytes = zf.read(pdf_name)
    label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"
    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"

    caption = f"📄 گزارش PDF {label} {subject}"[:1024]

    try:
        tg_document(chat_id, "TradingAgents_Report.pdf", pdf_bytes, caption)
    except Exception as exc:
        send_message(chat_id, f"❌ ارسال PDF ناموفق: {esc(exc)}", [[("بازگشت", "outputs")]])


def valid_date(value: str) -> str:
    value = value.strip().lower()
    if value in ("today", "امروز"):
        return dt.date.today().isoformat()

    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("❌ فرمت اشتباه.\n✅ YYYY-MM-DD\nمثال: 2026-10-03") from exc

    if day > dt.date.today():
        raise ValueError("تاریخ نمی‌تواند در آینده باشد.")

    return day.isoformat()


def valid_ticker(value: str, allow_many: bool = False) -> str:
    raw = value.strip().upper().replace("$", "")

    if allow_many:
        parts = [x for x in raw.replace(",", " ").split() if x]
        if not parts:
            raise ValueError("حداقل یک Ticker وارد کن.")

        values = []
        for part in parts:
            if not all(ch.isalnum() or ch in "._-^=" for ch in part) or len(part) > 32:
                raise ValueError(f"❌ Ticker نامعتبر: {part}")
            if part not in values:
                values.append(part)

        return ",".join(values)

    if not raw or any(ch.isspace() for ch in raw):
        raise ValueError("❌ Ticker خالی.\n✅ مثال: NVDA")

    if not all(ch.isalnum() or ch in "._-^=" for ch in raw) or len(raw) > 32:
        raise ValueError("فرمت Ticker نامعتبر.")

    return raw


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith(("-USD", "-USDT", "-USDC", "-BTC", "-ETH"))


def format_run_time(iso_str: str) -> str:
    try:
        t = dt.datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        delta = dt.datetime.now(dt.timezone.utc) - t
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "کمتر از ۱ دقیقه پیش"
        if minutes < 60:
            return f"{minutes} دقیقه پیش"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} ساعت پیش"
        return f"{hours // 24} روز پیش"
    except Exception:
        return ""


def home_screen(chat_id: int, force_new: bool = False, target_mid: int | None = None) -> int:
    aid = active_model_id()
    mrow = q1("SELECT name FROM models WHERE id=?", (aid,)) if aid else None
    quick_id, deep_id = tier_model_id("quick"), tier_model_id("deep")
    quick_row = q1("SELECT name FROM models WHERE id=? AND enabled=1", (quick_id,)) if quick_id else None
    deep_row = q1("SELECT name FROM models WHERE id=? AND enabled=1", (deep_id,)) if deep_id else None
    model_line = (
        f"⚡ Quick: <b>{esc(quick_row['name']) if quick_row else 'تنظیم نشده'}</b>\n"
        f"🧠 Deep: <b>{esc(deep_row['name']) if deep_row else 'تنظیم نشده'}</b>"
    )

    busy_row = q1(
        "SELECT COUNT(*) c FROM runs WHERE status NOT IN " + str(TERMINAL) + " AND chat_id=?",
        (chat_id,),
    )
    busy = int(busy_row["c"]) if busy_row else 0
    live = f"\n🟡 اجرای فعال شما: <b>{busy}</b>" if busy else ""

    text = (
        f"<b>🤖 TradingAgentsArya</b>\n"
        f"{model_line}{live}\n"
        "به سیستم هوشمند تحلیل و مدیریت معاملات خوش آمدید.\n"
        f"<code>build {BUILD}</code>"
    )

    rows = [
        [("🚀 تحلیل جدید", "flow_analysis_start"), ("📈 بک‌تست", "flow_backtest_start")],
        [("🤖 مدل‌ها", "models"), ("📊 اجراهای جاری", "active_runs")],
        [("📄 خروجی‌ها", "outputs")],
        [("⚙️ تنظیمات موتور", "engine_settings"), ("🎚 نقش مدل‌ها", "model_roles")],
    ]

    if force_new:
        new_mid = send_message(chat_id, text, rows)
        remember_ui(chat_id, new_mid)
        return new_mid

    return show(chat_id, text, rows, target_mid)


def models_screen(chat_id: int, page: int = 0, target_mid: int | None = None) -> int:
    rows = model_rows()
    aid = active_model_id()
    per_page = 3
    start = page * per_page
    end = start + per_page
    page_rows = rows[start:end]
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)

    # Remember which page is on screen: the activate and delete handlers come
    # back to it, and they had no way to know it because the page only ever
    # lived in the callback that is gone by then.
    set_setting(f"last_models_page_{chat_id}", str(page))

    text = "<b>🤖 مدل‌ها</b>\nلیست مدل‌های هوش مصنوعی ثبت‌شده در سیستم.\n"
    buttons: list[list[tuple[str, str]]] = []

    if not page_rows:
        text += "\nهنوز مدلی ثبت نشده است."
    else:
        for i in range(0, len(page_rows), 2):
            row_pair = page_rows[i:i + 2]
            btn_row = []
            for j, r in enumerate(row_pair):
                idx_num = start + i + j + 1
                mark = "✅ " if r["id"] == aid else ""
                model_name = r["name"] or "بدون نام"
                btn_text = f"{mark}⚡ #{idx_num} {model_name}"
                btn_row.append((btn_text, f"activate:{r['id']}"))
            buttons.append(btn_row)

        nav_row = []
        if page > 0:
            nav_row.append(("◀️ قبلی", f"models_page:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(("بعدی ▶️", f"models_page:{page + 1}"))
        if nav_row:
            buttons.append(nav_row)

    buttons += [
        [("➕ افزودن مدل", "model_add"), ("📋 پرووایدرها", "providers_menu")],
        [("🎚 نقش Quick / Deep", "model_roles")],
        [("🗑 حذف مدل", "model_delete_menu")],
        [("🏠 خانه", "home")],
    ]

    return show(chat_id, text, buttons, target_mid)


def model_roles_screen(chat_id: int, target_mid: int | None = None) -> int:
    quick_id, deep_id = tier_model_id("quick"), tier_model_id("deep")
    quick = q1("SELECT name FROM models WHERE id=? AND enabled=1", (quick_id,)) if quick_id else None
    deep = q1("SELECT name FROM models WHERE id=? AND enabled=1", (deep_id,)) if deep_id else None
    return show(
        chat_id,
        "<b>🎚 نقش مدل‌ها</b>\n"
        f"⚡ Quick (تحلیل‌گران، پژوهش‌گران و معامله‌گر): <b>{esc(quick['name']) if quick else 'مدل پیش‌فرض تنظیم نشده'}</b>\n"
        f"🧠 Deep (مدیران پژوهش و پورتفولیو): <b>{esc(deep['name']) if deep else 'مدل پیش‌فرض تنظیم نشده'}</b>\n"
        "هر نقش می‌تواند از Provider و Endpoint مستقل استفاده کند.",
        [
            [("⚡ انتخاب مدل Quick", "assign_tier_menu:quick")],
            [("🧠 انتخاب مدل Deep", "assign_tier_menu:deep")],
            [("بازگشت", "models")],
        ],
        target_mid,
    )


def assign_tier_screen(chat_id: int, tier: str, target_mid: int | None = None) -> int:
    if tier not in {"quick", "deep"}:
        raise ValueError("نقش مدل نامعتبر است.")
    active = tier_model_id(tier)
    rows = q("SELECT id,name FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE")
    buttons = [[(f"{'✅ ' if r['id'] == active else ''}{r['name'][:48]}", f"assign_tier:{tier}:{r['id']}")] for r in rows]
    buttons.append([("بازگشت", "model_roles")])
    label = "Quick" if tier == "quick" else "Deep"
    return show(chat_id, f"<b>انتخاب مدل {label}</b>\nمدل موردنظر را انتخاب کن.", buttons, target_mid)


def settings_menu(chat_id: int, target_mid: int | None = None) -> int:
    cfg = get_run_settings()
    lines = [
        "<b>⚙️ تنظیمات موتور تحلیل</b>",
        f"زبان گزارش: <b>{esc(cfg['output_language'])}</b>",
        f"عمق مناظره: <b>{cfg['max_debate_rounds']}</b> | ریسک: <b>{cfg['max_risk_discuss_rounds']}</b>",
        f"حد ابزار هر Agent: <b>{cfg['max_tool_rounds']}</b>",
        f"حد خروجی: <b>{esc(cfg['max_tokens'] if cfg['max_tokens'] is not None else 'پیش‌فرض Provider')}</b>",
        f"Temperature: <b>{esc(cfg['temperature'] if cfg['temperature'] is not None else 'پیش‌فرض Provider')}</b>",
        f"Checkpoint: <b>{'روشن' if cfg['checkpoint_enabled'] else 'خاموش'}</b>",
        f"Holding period: <b>{cfg['holding_period_days']} روز معاملاتی</b>",
        f"Benchmark: <b>{esc(cfg['benchmark_ticker'] or 'خودکار بر اساس بازار')}</b>",
    ]
    buttons = [
        [("🌐 زبان خروجی", "setting_menu:output_language")],
        [("🗣 دور مناظره", "setting_menu:max_debate_rounds"), ("🛡 دور ریسک", "setting_menu:max_risk_discuss_rounds")],
        [("🔧 دور ابزار Agent", "setting_menu:max_tool_rounds"), ("🎯 Max Tokens", "setting_menu:max_tokens")],
        [("🌡 Temperature", "setting_menu:temperature"), ("💾 Checkpoint", "setting_menu:checkpoint_enabled")],
        [("🧠 Google Thinking", "setting_menu:google_thinking_level")],
        [("🧠 OpenAI Reasoning", "setting_menu:openai_reasoning_effort"), ("🧠 Anthropic Effort", "setting_menu:anthropic_effort")],
        [("📆 Holding Period", "setting_menu:holding_period_days"), ("🏁 Benchmark", "benchmark_input")],
        [("🗃 منابع داده", "data_vendors_menu")],
        [("♻️ بازنشانی تنظیمات", "settings_reset"), ("🏠 خانه", "home")],
    ]
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def setting_choices_screen(chat_id: int, key: str, target_mid: int | None = None) -> int:
    if key not in SETTING_CHOICES:
        raise ValueError("این تنظیم قابل تغییر نیست.")
    current = get_run_settings()
    labels = {
        "output_language": {"Persian": "فارسی", "English": "English", "German": "Deutsch", "French": "Français", "Spanish": "Español", "Arabic": "العربية", "Chinese": "中文", "Japanese": "日本語"},
        "checkpoint_enabled": {True: "روشن", False: "خاموش"},
    }
    buttons = []
    for value in SETTING_CHOICES[key]:
        token = "default" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        label = labels.get(key, {}).get(value, "پیش‌فرض Provider" if value is None else str(value))
        selected = "✅ " if current.get(key) == value else ""
        buttons.append([(selected + label, f"setting:{key}:{token}")])
    buttons.append([("بازگشت", "engine_settings")])
    return show(chat_id, f"<b>انتخاب مقدار</b>\n<code>{esc(key)}</code>", buttons, target_mid)


def data_vendors_menu(chat_id: int, target_mid: int | None = None) -> int:
    cfg = get_run_settings()
    names = {
        "core_stock_apis": "قیمت و داده‌های اصلی",
        "technical_indicators": "اندیکاتورهای تکنیکال",
        "fundamental_data": "داده بنیادی",
        "news_data": "اخبار",
        "macro_data": "داده کلان",
        "prediction_markets": "بازارهای پیش‌بینی",
    }
    lines = ["<b>🗃 منابع داده</b>"]
    buttons = []
    for key, title in names.items():
        lines.append(f"{title}: <code>{esc(cfg['data_vendors'][key])}</code>")
        buttons.append([(title, f"vendor_menu:{key}")])
    buttons.append([("بازگشت", "engine_settings")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def data_vendor_choices_screen(chat_id: int, category: str, target_mid: int | None = None) -> int:
    if category not in DATA_VENDOR_OPTIONS:
        raise ValueError("دسته منبع داده نامعتبر است.")
    current = get_run_settings()["data_vendors"][category]
    buttons = []
    for vendor in DATA_VENDOR_OPTIONS[category]:
        label = vendor.replace(",", " + ")
        buttons.append([(f"{'✅ ' if vendor == current else ''}{label}", f"vendor:{category}:{vendor}")])
    buttons.append([("بازگشت", "data_vendors_menu")])
    return show(chat_id, f"<b>انتخاب منبع داده</b>\n<code>{esc(category)}</code>", buttons, target_mid)


def portfolio_json_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>🎒 پورتفولیو (JSON)</b>\n"
        "یک JSON با ساختار زیر بفرست. اطلاعات فقط همراه همین اجرا و به‌صورت رمزنگاری‌شده منتقل می‌شود.\n"
        "<pre>{\n  \"cash\": 10000,\n  \"currency\": \"USD\",\n  \"positions\": [{\"ticker\": \"NVDA\", \"quantity\": 5, \"average_price\": 120}]\n}</pre>",
        [[("❌ لغو ورود", "portfolio_cancel")]],
        target_mid,
    )


def providers_menu(chat_id: int, target_mid: int | None = None) -> int:
    provs = q("SELECT * FROM providers ORDER BY name")
    if not provs:
        return show(
            chat_id,
            "📭 هیچ Provider ذخیره‌شده‌ای وجود ندارد.",
            [[("➕ افزودن مدل", "model_add"), ("🏠 خانه", "home")]],
            target_mid,
        )

    text = "<b>📋 پرووایدرها</b>\nلیست ارائه‌دهندگان سرویس هوش مصنوعی."
    buttons = []

    for i in range(0, len(provs), 2):
        row_provs = provs[i:i + 2]
        row_buttons = []
        for p in row_provs:
            row_buttons.append((f"🔹 {esc(p['name'])}", f"provider_detail:{p['id']}"))
        buttons.append(row_buttons)

    buttons.append([("🏠 خانه", "home")])
    return show(chat_id, text, buttons, target_mid)


def provider_detail_screen(chat_id: int, prov_id: str, target_mid: int | None = None) -> int:
    prov = get_provider(prov_id)
    if not prov:
        return show(chat_id, "❌ Provider یافت نشد.", [[("بازگشت", "providers_menu")]], target_mid)

    return show(
        chat_id,
        f"<b>🔹 {esc(prov['name'])}</b>\n"
        f"Type: <code>{esc(prov['provider_type'])}</code>\n"
        f"URL: <code>{esc(prov['base_url'])}</code>",
        [
            [("📋 لیست مدل‌ها", f"list_provider_models:{prov_id}")],
            [("🗑 حذف این Provider", f"ask_delete_provider:{prov_id}")],
            [("بازگشت", "providers_menu")],
        ],
        target_mid,
    )


def ask_delete_provider_screen(chat_id: int, prov_id: str, target_mid: int | None = None) -> int:
    prov = get_provider(prov_id)
    if not prov:
        return show(chat_id, "❌ Provider یافت نشد.", [[("بازگشت", "providers_menu")]], target_mid)

    count = q1("SELECT COUNT(*) c FROM models WHERE provider_id=?", (prov_id,))
    count_val = int(count["c"]) if count else 0

    return show(
        chat_id,
        f"<b>⚠️ حذف Provider</b>\n"
        f"آیا مطمئنی می‌خواهی <b>{esc(prov['name'])}</b> و <b>{count_val}</b> مدل مرتبط را حذف کنی؟\n"
        "این عمل غیرقابل بازگشت است.",
        [
            [("✅ بله، حذف شود", f"confirm_delete_provider:{prov_id}")],
            [("❌ انصراف", f"provider_detail:{prov_id}")],
        ],
        target_mid,
    )


def list_provider_models_screen(chat_id: int, prov_id: str, page: int = 0, target_mid: int | None = None) -> int:
    prov = get_provider(prov_id)
    if not prov:
        return show(chat_id, "❌ Provider یافت نشد.", [[("بازگشت", "providers_menu")]], target_mid)

    token = decrypt_token(prov["token_ciphertext"])
    models = fetch_models_list(prov["base_url"], token)

    if not models:
        return show(
            chat_id,
            f"📭 لیست مدل‌ها برای <b>{esc(prov['name'])}</b> دریافت نشد.\nلطفاً Model ID را دستی وارد کن.",
            [[("✏️ ورود دستی", f"manual_model_for_prov:{prov_id}"), ("بازگشت", "providers_menu")]],
            target_mid,
        )

    WIZARDS[chat_id] = {"stage": "provider_models", "prov_id": prov_id, "models_list": models}

    per_page = 12
    start = page * per_page
    end = start + per_page
    page_models = models[start:end]
    total_pages = max(1, (len(models) + per_page - 1) // per_page)

    text = f"<b>📋 مدل‌های {esc(prov['name'])} ({len(models)} مورد)</b>\nصفحه {page + 1} از {total_pages}\n"
    buttons = []

    for i in range(0, len(page_models), 2):
        row_models = page_models[i:i + 2]
        row_buttons = []
        for idx_offset, model_id in enumerate(row_models):
            global_idx = start + i + idx_offset
            short_name = model_id.split("/")[-1] if "/" in model_id else model_id
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            row_buttons.append((short_name, f"md:{prov_id}:{global_idx}"))
        buttons.append(row_buttons)

    nav_row = []
    if page > 0:
        nav_row.append(("◀️ قبلی", f"prov_models_page:{prov_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(("بعدی ▶️", f"prov_models_page:{prov_id}:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([("✏️ ورود دستی", f"manual_model_for_prov:{prov_id}"), ("بازگشت", "providers_menu")])
    return show(chat_id, text, buttons, target_mid)


def model_detail_screen(chat_id: int, prov_id: str, model_name: str, idx: int, temp: bool = False, target_mid: int | None = None) -> int:
    if temp:
        test_cb = f"mdt_test:{idx}"
        select_cb = f"mdt_select:{idx}"
        back_cb = "models_page_list:0"
    else:
        test_cb = f"pmd_test:{prov_id}:{idx}"
        select_cb = f"pmd_select:{prov_id}:{idx}"
        back_cb = f"list_provider_models:{prov_id}"

    return show(
        chat_id,
        f"<b>📋 جزئیات مدل</b>\nModel: <code>{esc(model_name)}</code>\nیک عملیات را انتخاب کن:",
        [
            [("🧪 تست اتصال", test_cb)],
            [("✅ انتخاب و فعال‌سازی", select_cb)],
            [("بازگشت", back_cb)],
        ],
        target_mid,
    )


def wizard_url_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>➕ افزودن مدل (1 از 3)</b>\nBase URL را بفرست.\n<b>مثال:</b> <code>https://integrate.api.nvidia.com/v1</code>",
        [[("❌ لغو", "wizard_cancel")]],
        target_mid,
    )


def wizard_token_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>➕ افزودن مدل (2 از 3)</b>\nAPI Token را بفرست.\nاگر سرویس توکن نمی‌خواهد، کلمه <code>-</code> را بفرست.",
        [[("❌ لغو", "wizard_cancel")]],
        target_mid,
    )


def wizard_model_manual_screen(chat_id: int, target_mid: int | None = None) -> int:
    wizard = WIZARDS.get(chat_id, {})
    provider = wizard.get("provider_name", "Custom")
    return show(
        chat_id,
        f"<b>➕ افزودن مدل (3 از 3)</b>\nModel ID را بفرست.\n<b>Provider:</b> <code>{esc(provider)}</code>\nیا دکمه زیر را بزن:",
        [[("📋 دریافت لیست مدل‌ها", "fetch_models_list"), ("❌ لغو", "wizard_cancel")]],
        target_mid,
    )


def models_list_screen(chat_id: int, page: int = 0, target_mid: int | None = None) -> int:
    wizard = WIZARDS.get(chat_id, {})
    models = wizard.get("models_list", [])
    prov_id = wizard.get("prov_id", "")

    if not models:
        return show(
            chat_id,
            "📭 لیست مدل‌ها خالی است.",
            [[("✏️ ورود دستی", "wizard_manual_model"), ("بازگشت", "models")]],
            target_mid,
        )

    per_page = 12
    start = page * per_page
    end = start + per_page
    page_models = models[start:end]
    total_pages = max(1, (len(models) + per_page - 1) // per_page)

    text = f"<b>📋 لیست مدل‌ها ({len(models)} مورد)</b>\nصفحه {page + 1} از {total_pages}\n"
    buttons = []

    for i in range(0, len(page_models), 2):
        row_models = page_models[i:i + 2]
        row_buttons = []
        for idx_offset, model_id in enumerate(row_models):
            global_idx = start + i + idx_offset
            short_name = model_id.split("/")[-1] if "/" in model_id else model_id
            if len(short_name) > 20:
                short_name = short_name[:17] + "..."
            row_buttons.append((short_name, f"md_temp:{global_idx}"))
        buttons.append(row_buttons)

    nav_row = []
    if page > 0:
        nav_row.append(("◀️ قبلی", f"models_page_list:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(("بعدی ▶️", f"models_page_list:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([("✏️ ورود دستی", "wizard_manual_model"), ("بازگشت", "models")])
    return show(chat_id, text, buttons, target_mid)


def model_test_menu(chat_id: int, target_mid: int | None = None) -> int:
    buttons = [[(f"🧪 {r['name']}", f"test_model:{r['id']}")] for r in model_rows()]
    buttons.append([("بازگشت", "models")])
    return show(
        chat_id,
        "<b>🧪 تست اتصال</b>\nیک درخواست بسیار کوچک واقعی به API؛ TradingAgents اجرا نمی‌شود.",
        buttons,
        target_mid,
    )


def model_delete_menu(chat_id: int, target_mid: int | None = None) -> int:
    buttons = [[(f"🗑 {r['name']}", f"delete_model:{r['id']}")] for r in model_rows()]
    buttons.append([("بازگشت", "models")])
    return show(chat_id, "<b>🗑 حذف مدل</b>\nمدل را انتخاب کن.", buttons, target_mid)


def analysis_ticker_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>🔎 تحلیل جدید (1/3)</b>\nTicker را بفرست.\n<b>مثال:</b> <code>NVDA</code>",
        [[("❌ لغو", "flow_cancel")]],
        target_mid,
    )


def analysis_date_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>🔎 تحلیل جدید (2/3)</b>\nتاریخ را بفرست.\n<b>فرمت:</b> YYYY-MM-DD\n<b>مثال:</b> <code>2026-10-03</code>",
        [[("📅 امروز", "analysis_today"), ("❌ لغو", "flow_cancel")]],
        target_mid,
    )


def backtest_tickers_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>📈 بک‌تست (1/5)</b>\nTickerها را بفرست.\n<b>مثال:</b> <code>NVDA,AAPL</code>",
        [[("❌ لغو", "flow_cancel")]],
        target_mid,
    )


def backtest_start_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>📈 بک‌تست (2/5)</b>\nتاریخ شروع را بفرست.\n<b>مثال:</b> <code>2026-01-01</code>",
        [[("❌ لغو", "flow_cancel")]],
        target_mid,
    )


def backtest_end_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>📈 بک‌تست (3/5)</b>\nتاریخ پایان را بفرست.\n<b>مثال:</b> <code>2026-10-01</code>",
        [[("📅 امروز", "backtest_today"), ("❌ لغو", "flow_cancel")]],
        target_mid,
    )


def backtest_every_screen(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>📈 بک‌تست (4/5)</b>\nفاصله زمانی:",
        [
            [("📅 1 روز", "every:1"), ("📅 7 روز", "every:7")],
            [("📅 14 روز", "every:14"), ("📅 30 روز", "every:30")],
            [("❌ لغو", "flow_cancel")],
        ],
        target_mid,
    )


def analyst_picker(chat_id: int, backtest: bool, target_mid: int | None = None) -> int:
    data = FLOWS.get(chat_id, {})
    allowed = list(ANALYST_ORDER)
    ticker = data.get("ticker", "")

    if (not backtest and ticker and is_crypto(ticker)) or (backtest and data.get("asset_type") == "crypto"):
        allowed.remove("fundamentals")

    selected = {x for x in data.get("analysts", "").split(",") if x in allowed}
    data["analysts"] = ",".join(x for x in allowed if x in selected)

    rows = []
    for i in range(0, len(allowed), 2):
        pair = allowed[i:i + 2]
        rows.append([(("✅ " if x in selected else "") + ANALYST_LABEL[x], f"toggle_analyst:{x}") for x in pair])

    rows += [[("✅ تایید", "analysts_done")], [("❌ لغو", "flow_cancel")]]
    return show(chat_id, "<b>🎛 تحلیلگران</b>", rows, target_mid)


def analysis_confirm_screen(chat_id: int, target_mid: int | None = None) -> int:
    data = FLOWS.get(chat_id, {})
    chosen = ", ".join(ANALYST_LABEL[x] for x in data.get("analysts", "").split(",") if x in ANALYST_LABEL) or "هیچ‌کدام"

    return show(
        chat_id,
        f"<b>✅ آماده اجرا</b>\n"
        f"Ticker: <code>{esc(data.get('ticker'))}</code>\n"
        f"تاریخ: <code>{esc(data.get('date'))}</code>\n"
        f"تحلیلگران: <b>{esc(chosen)}</b>\n"
        f"پورتفولیو: <b>{'تنظیم‌شده' if data.get('portfolio') else 'بدون اطلاعات پورتفولیو'}</b>",
        [
            [("🚀 شروع", "analysis_run")],
            [("🎛 تحلیلگران", "analysis_analysts"), ("📅 تاریخ", "analysis_change_date")],
            [("🎒 پورتفولیو JSON", "portfolio_input"), ("🧹 حذف پورتفولیو", "portfolio_clear")],
            [("❌ لغو", "flow_cancel")],
        ],
        target_mid,
    )


def backtest_confirm_screen(chat_id: int, target_mid: int | None = None) -> int:
    data = FLOWS.get(chat_id, {})
    chosen = ", ".join(ANALYST_LABEL[x] for x in data.get("analysts", "").split(",") if x in ANALYST_LABEL) or "هیچ‌کدام"

    return show(
        chat_id,
        f"<b>✅ آماده بک‌تست</b>\n"
        f"Tickerها: <code>{esc(data.get('tickers'))}</code>\n"
        f"از <code>{esc(data.get('start'))}</code> تا <code>{esc(data.get('end'))}</code>\n"
        f"فاصله: <b>هر {esc(data.get('every', 7))} روز</b>\n"
        f"نوع دارایی: <b>{esc(data.get('asset_type', 'stock'))}</b>\n"
        f"تحلیلگران: <b>{esc(chosen)}</b>\n"
        f"پورتفولیو: <b>{'تنظیم‌شده' if data.get('portfolio') else 'بدون اطلاعات پورتفولیو'}</b>",
        [
            [("🚀 شروع", "backtest_run")],
            [("🎛 تحلیلگران", "backtest_analysts"), ("📅 فاصله", "backtest_every")],
            [("💱 نوع دارایی", "backtest_asset_type")],
            [("🎒 پورتفولیو JSON", "portfolio_input"), ("🧹 حذف پورتفولیو", "portfolio_clear")],
            [("❌ لغو", "flow_cancel")],
        ],
        target_mid,
    )


def _request_id_from_title(title: str) -> str:
    """The engine workflow names every run 'Engine · <request_id>'."""
    text = str(title or "")
    if "·" in text:
        text = text.split("·", 1)[1]
    return text.strip()


def _subject_of(payload_json: str) -> str:
    try:
        params = json.loads(payload_json or "{}")
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}
    raw = params.get("ticker") or params.get("tickers") or "—"
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(item) for item in raw) or "—"
    return str(raw)


def _collect_active_runs(chat_id: int, api_runs: list, db_rows: list, is_admin: bool = False) -> list[dict]:
    """Merge live workflow runs and local run rows into one list for this chat.

    A live run is attributed to a chat through its local row, because the
    request id is part of the workflow run name. Rows without a live
    counterpart are kept too, so a run that the API does not list yet (or that
    finished between two refreshes) is never silently dropped.
    """
    rows_by_request: dict[str, dict] = {}
    for row in db_rows or []:
        data = dict(row)
        rows_by_request[str(data.get("request_id") or "")] = data

    entries: list[dict] = []
    seen: set[str] = set()

    for run in api_runs or []:
        request_id = _request_id_from_title(run.get("display_title"))
        if not request_id or request_id in seen:
            continue
        row = rows_by_request.get(request_id)
        owner = int(row["chat_id"]) if row and row.get("chat_id") is not None else None
        if owner is not None and owner != chat_id and not is_admin:
            continue
        seen.add(request_id)
        entries.append(
            {
                "request_id": request_id,
                "workflow_run_id": run.get("id"),
                "status": str(run.get("status") or "queued"),
                "created_at": str(run.get("created_at") or (row or {}).get("created_at") or ""),
                "subject": _subject_of((row or {}).get("payload_json") or ""),
                "owner": owner,
                "known_owner": owner is not None,
                # Only a run this chat owns may be cancelled from this screen.
                "cancelable": bool(run.get("id")) and (owner == chat_id or is_admin),
                "stale": False,
            }
        )

    for request_id, row in rows_by_request.items():
        if not request_id or request_id in seen:
            continue
        status = str(row.get("status") or "queued")
        if status in TERMINAL:
            continue
        owner = row.get("chat_id")
        owner_id = int(owner) if owner is not None else None
        if owner_id is not None and owner_id != chat_id and not is_admin:
            continue
        entries.append(
            {
                "request_id": request_id,
                "workflow_run_id": row.get("workflow_run_id"),
                "status": status,
                "created_at": str(row.get("created_at") or ""),
                "subject": _subject_of(row.get("payload_json") or ""),
                "owner": owner_id,
                "known_owner": owner_id is not None,
                "cancelable": bool(row.get("workflow_run_id")) and (owner_id == chat_id or is_admin),
                "stale": True,
            }
        )

    queued = sorted(
        (entry for entry in entries if entry["status"] in QUEUE_STATUSES),
        key=lambda entry: str(entry["created_at"] or ""),
    )
    for position, entry in enumerate(queued, 1):
        entry["queue_position"] = position
    for entry in entries:
        entry.setdefault("queue_position", 0)

    running = sorted(
        (entry for entry in entries if entry["status"] not in QUEUE_STATUSES),
        key=lambda entry: str(entry["created_at"] or ""),
        reverse=True,
    )
    return running + queued


def _send_run_card(chat_id: int, mode: str, request_id: str, params: dict) -> None:
    """Announce a dispatched run in a message of its own.

    A chat keeps one shared UI message for the screens. Writing the
    confirmation there replaced the announcement of the previous run, so with
    several runs in flight only the last one was visible.
    """
    label = "تحلیل" if mode == "analysis" else "بک‌تست"
    raw_subject = params.get("ticker") or params.get("tickers") or "—"
    subject = (
        ", ".join(str(item) for item in raw_subject)
        if isinstance(raw_subject, (list, tuple))
        else str(raw_subject)
    )
    send_message(
        chat_id,
        f"<b>🚀 {esc(label)} ثبت شد</b>\n"
        f"شناسه: <code>{esc(request_id[:10])}</code>\n"
        f"موضوع: <b>{esc(subject)}</b>\n"
        f"وضعیت: 🕒 در صف\n"
        f"<i>این کارت مستقل است؛ با ثبت اجرای بعدی پاک نمی‌شود.</i>",
        [[("📊 اجراهای جاری", "active_runs"), ("🏠 خانه", "home")]],
    )


def active_runs_screen(chat_id: int, target_mid: int | None = None) -> int:
    api_runs = list(CACHE.get("runs", []))
    db_rows = [
        dict(row)
        for row in q(
            "SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 50"
        )
    ]
    entries = _collect_active_runs(chat_id, api_runs, db_rows, admin(chat_id))

    lines = [
        "<b>📊 اجراهای جاری</b>\nپردازش‌هایی که هم‌اکنون در حال اجرا یا در صف انتظار هستند.",
    ]
    buttons = []

    if entries:
        lines.append(f"\n<b>{len(entries)}</b> پردازش فعال")
        for idx, entry in enumerate(entries, 1):
            status = entry["status"]
            icon = "🟢" if status == "in_progress" else "🕒"
            label = "وضعیت نامشخص" if entry.get("stale") else RUN_STATUS_LABEL.get(status, status)
            line = f"<b>#{idx}</b> {icon} <b>{esc(entry['subject'])}</b> — {esc(label)}"
            if entry.get("queue_position"):
                line += f" · نوبت {entry['queue_position']}"
            elapsed = format_run_time(entry["created_at"])
            if elapsed:
                line += f" · ⏱ {esc(elapsed)}"
            if not entry.get("known_owner"):
                line += " · <i>مالک نامشخص</i>"
            lines.append(line)
            lines.append(f"<code>{esc(entry['request_id'][:10])}</code>")
            if entry.get("cancelable"):
                buttons.append([("🛑 لغو #" + str(idx), f"cancelw:{entry['workflow_run_id']}")])
            lines.append("")
    else:
        lines.append("\n✅ هیچ پردازش فعالی در حال حاضر وجود ندارد.")

    buttons.append([("🔄 تازه‌سازی", "active_runs"), ("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def outputs_screen(chat_id: int, page: int = 0, target_mid: int | None = None) -> int:
    with contextlib.suppress(Exception):
        sync_run_records()

    per_page = 3
    offset = page * per_page

    rows = q(
        "SELECT * FROM runs WHERE chat_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (chat_id, per_page, offset),
    )
    total_count = q1("SELECT COUNT(*) c FROM runs WHERE chat_id=?", (chat_id,))
    total_count_val = int(total_count["c"]) if total_count else 0
    total_pages = max(1, (total_count_val + per_page - 1) // per_page)

    if not rows:
        return show(
            chat_id,
            "<b>📄 خروجی‌ها</b>\nهنوز گزارشی برای نمایش وجود ندارد.",
            [[("🏠 خانه", "home")]],
            target_mid,
        )

    lines = [f"<b>📄 خروجی‌ها</b> (صفحه {page + 1}/{total_pages})\nلیست گزارش‌های تولیدشده.", ""]
    buttons = []

    for idx, row in enumerate(rows, start=offset + 1):
        params = json.loads(row["payload_json"] or "{}")
        subject = params.get("ticker") or params.get("tickers") or "—"

        try:
            created_str = dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_str = row["created_at"][:16]

        is_active = row["status"] not in TERMINAL
        status_icon = "🟡" if is_active else "✅" if row["status"] == "success" else "❌"

        line = f"<b>#{idx}</b> {status_icon} <code>{esc(row['request_id'][:8])}</code> <b>{esc(subject)}</b> | {esc(created_str)}"
        if is_active:
            label = RUN_STATUS_LABEL.get(row["status"], row["status"])
            elapsed = format_run_time(row["created_at"])
            line += f" · {esc(label)}" + (f" · ⏱ {esc(elapsed)}" if elapsed else "")
        lines.append(line)

        btn_row = [("📄 گزارش #" + str(idx), f"view_output:{row['request_id']}")]
        if is_active:
            # Re-render this page instead of a dead button, so the status of a
            # running row can be watched without leaving the list.
            btn_row.append(("🔄 تازه‌سازی", f"outputs_page:{page}"))
        else:
            btn_row.append(("🗑 حذف #" + str(idx), f"ask_delete:{row['request_id']}"))

        buttons.append(btn_row)

    nav_row = []
    if page > 0:
        nav_row.append(("◀️ قبلی", f"outputs_page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(("بعدی ▶️", f"outputs_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([("🧹 پاک‌سازی", "bulk_delete_menu"), ("🔄 تازه‌سازی", "outputs_refresh")])
    buttons.append([("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def ask_delete_screen(chat_id: int, request_id: str, target_mid: int | None = None) -> int:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row:
        return show(chat_id, "❌ پیدا نشد.", [[("بازگشت", "outputs")]], target_mid)

    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"

    return show(
        chat_id,
        f"<b>⚠️ حذف</b>\n<code>{esc(request_id[:10])}</code> | <b>{esc(subject)}</b>\nغیرقابل بازگشت.",
        [[("✅ بله", f"confirm_delete:{request_id}")], [("❌ انصراف", "outputs")]],
        target_mid,
    )


def bulk_delete_menu(chat_id: int, target_mid: int | None = None) -> int:
    return show(
        chat_id,
        "<b>🧹 پاک‌سازی</b>\nچه بازه‌ای از تاریخچه پاک شود؟",
        [
            [("🗑 ۷ روز", "bulk_scope:7"), ("🗑 ۳۰ روز", "bulk_scope:30")],
            [("🗑 همه", "bulk_scope:all")],
            [("❌ انصراف", "outputs")],
        ],
        target_mid,
    )


def bulk_confirm_screen(chat_id: int, scope: str, target_mid: int | None = None) -> int:
    label = {"7": "۷ روز", "30": "۳۰ روز", "all": "همه"}.get(scope, scope)
    return show(
        chat_id,
        f"<b>⚠️ پاک‌سازی {esc(label)}</b>\nمطمئنی؟",
        [[("✅ بله", f"bulk_do:{scope}")], [("❌ انصراف", "outputs")]],
        target_mid,
    )


def handle_text(chat_id: int, text: str, target_mid: int) -> None:
    text = text.strip()

    if text.lower() == "/cancel":
        result = hard_cancel(chat_id)
        send_message(chat_id, result, [[("🏠 خانه", "home")]])
        return

    if text.startswith("/start"):
        WIZARDS.pop(chat_id, None)
        FLOWS.pop(chat_id, None)
        home_screen(chat_id, force_new=True)
        return

    if chat_id in WIZARDS:
        item = WIZARDS[chat_id]
        stage = item.get("stage")

        try:
            if stage == "benchmark":
                cfg = get_run_settings()
                cfg["benchmark_ticker"] = text
                save_run_settings(cfg)
                WIZARDS.pop(chat_id, None)
                settings_menu(chat_id, target_mid)

            elif stage == "url":
                item["url"] = clean_base_url(text)
                item["provider_type"] = infer_provider_type(item["url"])
                item["provider_name"] = infer_provider_name(item["url"])
                item["stage"] = "token"
                wizard_token_screen(chat_id, target_mid)

            elif stage == "token":
                if item.get("provider_type") == "ollama" and text.strip().lower() in ("-", "none", "no", "empty"):
                    item["token"] = ""
                elif len(text) < 3:
                    raise ValueError("Token کوتاه است.")
                else:
                    item["token"] = text

                prov_id = save_provider(item["provider_name"], item["provider_type"], item["url"], item["token"])
                item["prov_id"] = prov_id
                item["stage"] = "model"

                wizard_model_manual_screen(chat_id, target_mid)

            elif stage == "model_manual":
                if not text:
                    raise ValueError("Model ID خالی.")

                prov_id = item.get("prov_id")
                if not prov_id:
                    raise ValueError("Provider یافت نشد.")

                model_id = add_model_from_provider(prov_id, text)
                WIZARDS.pop(chat_id, None)

                show(
                    chat_id,
                    f"<b>✅ مدل ذخیره و فعال شد</b>\n<code>{esc(text)}</code>",
                    [[("🧪 تست", f"test_model:{model_id}"), ("🏠 خانه", "home")]],
                    target_mid,
                )

        except ValueError as exc:
            show(chat_id, f"<b>⚠️ {esc(exc)}</b>\nلطفاً دوباره تلاش کنید.", [[("❌ لغو", "wizard_cancel")]], target_mid)

        return

    if chat_id in FLOWS:
        flow = FLOWS[chat_id]
        kind = flow.get("kind")
        stage = flow.get("stage")

        try:
            if kind == "analysis" and stage == "ticker":
                flow["ticker"] = valid_ticker(text)
                flow["stage"] = "date"
                analysis_date_screen(chat_id, target_mid)

            elif kind == "analysis" and stage == "date":
                flow["date"] = valid_date(text)
                flow["stage"] = "analysts"
                analyst_picker(chat_id, backtest=False, target_mid=target_mid)

            elif kind == "backtest" and stage == "tickers":
                flow["tickers"] = valid_ticker(text, allow_many=True)
                flow["stage"] = "start"
                backtest_start_screen(chat_id, target_mid)

            elif kind == "backtest" and stage == "start":
                flow["start"] = valid_date(text)
                flow["stage"] = "end"
                backtest_end_screen(chat_id, target_mid)

            elif kind == "backtest" and stage == "end":
                end = valid_date(text)
                if end < flow.get("start", end):
                    raise ValueError("پایان قبل از شروع.")
                flow["end"] = end
                flow["stage"] = "every"
                backtest_every_screen(chat_id, target_mid)

            elif stage == "portfolio":
                try:
                    raw_portfolio = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"JSON معتبر نیست: {exc.msg}") from exc
                flow["portfolio"] = validate_portfolio(raw_portfolio)
                flow["stage"] = "confirm"
                if kind == "analysis":
                    analysis_confirm_screen(chat_id, target_mid)
                else:
                    backtest_confirm_screen(chat_id, target_mid)

        except ValueError as exc:
            show(chat_id, f"<b>⚠️ {esc(exc)}</b>\nلطفاً دوباره تلاش کنید.", [[("❌ لغو", "flow_cancel")]], target_mid)


def callback(query: dict[str, Any]) -> None:
    user_id = int(query.get("from", {}).get("id", 0))
    if not authorized(user_id):
        answer_callback(query["id"], "دسترسی مجاز نیست.", True)
        return

    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    cb_mid = int(message.get("message_id", 0))
    data = query.get("data", "")

    if data.startswith("test_model:") or data.startswith("pmd_test:") or data.startswith("mdt_test:"):
        answer_callback(query["id"], "⏳ در حال تست...", False)
    elif data.startswith("view_output:"):
        answer_callback(query["id"], "📄 در حال آماده‌سازی...", False)
    elif data == "fetch_models_list":
        answer_callback(query["id"], "📋 در حال دریافت...", False)
    else:
        answer_callback(query["id"])

    try:
        if data == "home":
            WIZARDS.pop(chat_id, None)
            FLOWS.pop(chat_id, None)
            # The persistent UI message, not the pressed one: a run card is a
            # message of its own and must survive the navigation.
            home_screen(chat_id)

        elif data == "models":
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "engine_settings":
            settings_menu(chat_id, cb_mid)

        elif data.startswith("setting_menu:"):
            setting_choices_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("setting:"):
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند تنظیمات سراسری موتور را تغییر دهد.")
            _, key, value = data.split(":", 2)
            updated = apply_setting(get_run_settings(), key, value)
            save_run_settings(updated)
            settings_menu(chat_id, cb_mid)

        elif data == "settings_reset":
            if not admin(user_id):
                raise ValueError("فقط Admin.")
            save_run_settings({})
            settings_menu(chat_id, cb_mid)

        elif data == "data_vendors_menu":
            data_vendors_menu(chat_id, cb_mid)

        elif data.startswith("vendor_menu:"):
            data_vendor_choices_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("vendor:"):
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند منابع داده را تغییر دهد.")
            _, category, vendor = data.split(":", 2)
            updated = set_data_vendor(get_run_settings(), category, vendor)
            save_run_settings(updated)
            data_vendors_menu(chat_id, cb_mid)

        elif data == "benchmark_input":
            if not admin(user_id):
                raise ValueError("فقط Admin.")
            WIZARDS[chat_id] = {"stage": "benchmark"}
            show(chat_id, "<b>🏁 Benchmark سفارشی</b>\nنماد شاخص را بفرست؛ برای بازگشت به تشخیص خودکار، <code>-</code> بفرست.", [[("لغو", "benchmark_cancel")]], cb_mid)

        elif data == "benchmark_cancel":
            WIZARDS.pop(chat_id, None)
            settings_menu(chat_id, cb_mid)

        elif data == "model_roles":
            model_roles_screen(chat_id, cb_mid)

        elif data.startswith("assign_tier_menu:"):
            assign_tier_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("assign_tier:"):
            if not admin(user_id):
                raise ValueError("فقط Admin.")
            _, tier, model_id = data.split(":", 2)
            set_tier_model_id(tier, model_id)
            model_roles_screen(chat_id, cb_mid)

        elif data.startswith("models_page:"):
            page = int(data.split(":", 1)[1])
            models_screen(chat_id, page=page, target_mid=cb_mid)

        elif data == "providers_menu":
            providers_menu(chat_id, cb_mid)

        elif data.startswith("provider_detail:"):
            provider_detail_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("ask_delete_provider:"):
            ask_delete_provider_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("confirm_delete_provider:"):
            delete_provider(data.split(":", 1)[1])
            answer_callback(query["id"], "🗑 Provider حذف شد.", True)
            providers_menu(chat_id, cb_mid)

        elif data.startswith("list_provider_models:"):
            list_provider_models_screen(chat_id, data.split(":", 1)[1], page=0, target_mid=cb_mid)

        elif data.startswith("prov_models_page:"):
            parts = data.split(":")
            list_provider_models_screen(chat_id, parts[1], page=int(parts[2]), target_mid=cb_mid)

        elif data.startswith("md:"):
            parts = data.split(":")
            prov_id = parts[1]
            idx = int(parts[2])

            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])

            if wizard.get("prov_id") != prov_id or idx < 0 or idx >= len(models):
                raise ValueError("لیست مدل‌ها منقضی شده است.")

            model_detail_screen(chat_id, prov_id, models[idx], idx, temp=False, target_mid=cb_mid)

        elif data.startswith("md_temp:"):
            idx = int(data.split(":", 1)[1])
            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            prov_id = wizard.get("prov_id", "")

            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد.")

            model_detail_screen(chat_id, prov_id, models[idx], idx, temp=True, target_mid=cb_mid)

        elif data.startswith("pmd_detail:"):
            parts = data.split(":")
            prov_id = parts[1]
            idx = int(parts[2])

            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])

            if wizard.get("prov_id") != prov_id or idx < 0 or idx >= len(models):
                raise ValueError("لیست مدل‌ها منقضی شده است.")

            model_detail_screen(chat_id, prov_id, models[idx], idx, temp=False, target_mid=cb_mid)

        elif data.startswith("pmd_test:"):
            parts = data.split(":")
            prov_id = parts[1]
            idx = int(parts[2])

            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])

            if wizard.get("prov_id") != prov_id or idx < 0 or idx >= len(models):
                raise ValueError("لیست مدل‌ها منقضی شده است.")

            prov = get_provider(prov_id)
            if not prov:
                raise ValueError("Provider یافت نشد.")

            token = decrypt_token(prov["token_ciphertext"])
            threading.Thread(
                target=run_model_test_async,
                args=(chat_id, models[idx], prov["base_url"], token, prov["provider_type"], f"pmd_detail:{prov_id}:{idx}"),
                daemon=True,
            ).start()

        elif data.startswith("pmd_select:"):
            parts = data.split(":")
            prov_id = parts[1]
            idx = int(parts[2])

            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])

            if wizard.get("prov_id") != prov_id or idx < 0 or idx >= len(models):
                raise ValueError("لیست مدل‌ها منقضی شده است.")

            add_model_from_provider(prov_id, models[idx])
            answer_callback(query["id"], "✅ مدل فعال شد!", True)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("mdt_detail:"):
            idx = int(data.split(":", 1)[1])
            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            prov_id = wizard.get("prov_id", "")

            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد.")

            model_detail_screen(chat_id, prov_id, models[idx], idx, temp=True, target_mid=cb_mid)

        elif data.startswith("mdt_test:"):
            idx = int(data.split(":", 1)[1])
            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            prov_id = wizard.get("prov_id", "")

            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد.")

            prov = get_provider(prov_id)
            if not prov:
                raise ValueError("Provider یافت نشد.")

            token = decrypt_token(prov["token_ciphertext"])
            threading.Thread(
                target=run_model_test_async,
                args=(chat_id, models[idx], prov["base_url"], token, prov["provider_type"], f"mdt_detail:{idx}"),
                daemon=True,
            ).start()

        elif data.startswith("mdt_select:"):
            idx = int(data.split(":", 1)[1])
            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            prov_id = wizard.get("prov_id", "")

            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد.")

            add_model_from_provider(prov_id, models[idx])
            answer_callback(query["id"], "✅ مدل فعال شد!", True)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("manual_model_for_prov:"):
            prov_id = data.split(":", 1)[1]
            prov = get_provider(prov_id)
            if not prov:
                raise ValueError("Provider یافت نشد.")

            WIZARDS[chat_id] = {
                "stage": "model_manual",
                "prov_id": prov_id,
                "url": prov["base_url"],
                "token": decrypt_token(prov["token_ciphertext"]),
                "provider_name": prov["name"],
                "provider_type": prov["provider_type"],
            }
            wizard_model_manual_screen(chat_id, cb_mid)

        elif data == "model_add":
            if not admin(user_id):
                raise ValueError("فقط Admin.")
            WIZARDS[chat_id] = {"stage": "url"}
            wizard_url_screen(chat_id, cb_mid)

        elif data == "wizard_cancel":
            WIZARDS.pop(chat_id, None)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "wizard_manual_model":
            if chat_id in WIZARDS:
                WIZARDS[chat_id]["stage"] = "model_manual"
            wizard_model_manual_screen(chat_id, cb_mid)

        elif data == "fetch_models_list":
            wizard = WIZARDS.get(chat_id, {})
            prov_id = wizard.get("prov_id", "")

            if not prov_id:
                raise ValueError("Provider یافت نشد.")

            prov = get_provider(prov_id)
            if not prov:
                raise ValueError("Provider یافت نشد.")

            token = decrypt_token(prov["token_ciphertext"])
            models = fetch_models_list(prov["base_url"], token)

            if not models:
                show(chat_id, "📭 لیست خالی.", [[("✏️ دستی", "wizard_manual_model"), ("بازگشت", "models")]], cb_mid)
            else:
                wizard["models_list"] = models
                wizard["stage"] = "model_list"
                models_list_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("models_page_list:"):
            page = int(data.split(":", 1)[1])
            models_list_screen(chat_id, page=page, target_mid=cb_mid)

        elif data.startswith("test_model:"):
            model_id = data.split(":", 1)[1]
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,))
            if not row:
                raise ValueError("مدل پیدا نشد.")

            prov = get_provider(row["provider_id"])
            if not prov:
                raise ValueError("Provider یافت نشد.")

            token = decrypt_token(prov["token_ciphertext"])
            threading.Thread(
                target=run_model_test_async,
                args=(chat_id, row["name"], prov["base_url"], token, prov["provider_type"], "model_test_menu"),
                daemon=True,
            ).start()

        elif data.startswith("activate:"):
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (data.split(":", 1)[1],))
            if not row:
                raise ValueError("مدل پیدا نشد.")

            with contextlib.suppress(Exception):
                write_active_model_secrets(row)

            set_active_model(row["id"])
            answer_callback(query["id"], "⚡ مدل فعال شد!", True)

            last_page = int(get_setting(f"last_models_page_{chat_id}", "0"))
            models_screen(chat_id, page=last_page, target_mid=cb_mid)

        elif data == "model_delete_menu":
            if not admin(user_id):
                raise ValueError("فقط Admin.")
            model_delete_menu(chat_id, cb_mid)

        elif data.startswith("delete_model:"):
            if not admin(user_id):
                raise ValueError("فقط Admin.")

            model_id = data.split(":", 1)[1]
            db("UPDATE models SET enabled=0 WHERE id=?", (model_id,))

            if active_model_id() == model_id:
                set_active_model("")

            answer_callback(query["id"], "🗑 حذف شد.", True)

            last_page = int(get_setting(f"last_models_page_{chat_id}", "0"))
            models_screen(chat_id, page=last_page, target_mid=cb_mid)

        elif data == "active_runs":
            # Rendered in the persistent UI message so a run card (a message of
            # its own) is not overwritten by the list.
            active_runs_screen(chat_id)

        elif data.startswith("cancelw:"):
            run_id = int(data.split(":", 1)[1])
            try:
                outcome = cancel_run(run_id)
            except Exception as exc:
                # The run keeps going, so the button says so instead of
                # pretending it stopped.
                answer_callback(query["id"], f"❌ لغو ناموفق: {exc}", True)
                active_runs_screen(chat_id)
                return
            mark_run_cancelled(run_id, outcome)
            if outcome == "cancelled":
                answer_callback(query["id"], "🛑 لغو شد.", True)
            else:
                answer_callback(query["id"], "⏳ لغو ثبت شد، اجرا هنوز در حال توقف است.", True)
            active_runs_screen(chat_id)

        elif data == "outputs":
            outputs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "outputs_refresh":
            outputs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("outputs_page:"):
            page = int(data.split(":", 1)[1])
            outputs_screen(chat_id, page=page, target_mid=cb_mid)

        elif data.startswith("view_output:"):
            threading.Thread(target=send_output, args=(chat_id, data.split(":", 1)[1]), daemon=True).start()

        elif data.startswith("ask_delete:"):
            ask_delete_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("confirm_delete:"):
            db("DELETE FROM runs WHERE request_id=?", (data.split(":", 1)[1],))
            answer_callback(query["id"], "✅ حذف شد.", True)
            outputs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "bulk_delete_menu":
            bulk_delete_menu(chat_id, cb_mid)

        elif data.startswith("bulk_scope:"):
            bulk_confirm_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("bulk_do:"):
            scope = data.split(":", 1)[1]
            # Every run belongs to a chat; clearing "all" meant clearing every
            # user's runs, including the ones they cannot see in their own list.
            if scope == "all":
                cur = db("DELETE FROM runs WHERE chat_id=?", (chat_id,))
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(scope))).isoformat()
                cur = db("DELETE FROM runs WHERE chat_id=? AND created_at < ?", (chat_id, cutoff))

            answer_callback(query["id"], f"✅ {cur.rowcount if cur else 0} حذف شد.", True)
            outputs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data in ("logs", "logs_refresh") or data.startswith("logs_page:") or data.startswith("download_log:"):
            home_screen(chat_id, target_mid=cb_mid)

        elif data == "flow_analysis_start":
            if not active_model_id():
                raise ValueError("اول مدل فعال کن.")
            FLOWS[chat_id] = {"kind": "analysis", "stage": "ticker", "analysts": "market,social,news,fundamentals"}
            analysis_ticker_screen(chat_id, cb_mid)

        elif data == "flow_backtest_start":
            if not (tier_model_id("quick") and tier_model_id("deep")):
                raise ValueError("ابتدا مدل‌های Quick و Deep را در بخش نقش مدل‌ها تنظیم کن.")
            FLOWS[chat_id] = {"kind": "backtest", "stage": "tickers", "every": 7, "asset_type": "stock", "analysts": "market,social,news,fundamentals"}
            backtest_tickers_screen(chat_id, cb_mid)

        elif data == "portfolio_input":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("ابتدا یک تحلیل یا بک‌تست بساز.")
            flow["stage"] = "portfolio"
            portfolio_json_screen(chat_id, cb_mid)

        elif data == "portfolio_clear":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("اجرا پیدا نشد.")
            flow.pop("portfolio", None)
            if flow.get("kind") == "analysis":
                analysis_confirm_screen(chat_id, cb_mid)
            else:
                backtest_confirm_screen(chat_id, cb_mid)

        elif data == "portfolio_cancel":
            flow = FLOWS.get(chat_id)
            if not flow:
                home_screen(chat_id, cb_mid)
            elif flow.get("kind") == "analysis":
                flow["stage"] = "confirm"
                analysis_confirm_screen(chat_id, cb_mid)
            else:
                flow["stage"] = "confirm"
                backtest_confirm_screen(chat_id, cb_mid)

        elif data == "backtest_asset_type":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("اجرا منقضی شده است.")
            show(chat_id, "<b>نوع دارایی بک‌تست</b>", [[("📈 سهام", "asset_type:stock"), ("₿ کریپتو", "asset_type:crypto")], [("بازگشت", "noop")]], cb_mid)

        elif data.startswith("asset_type:"):
            flow = FLOWS.get(chat_id)
            if not flow or flow.get("kind") != "backtest":
                raise ValueError("بک‌تست منقضی شده است.")
            asset_type = data.split(":", 1)[1]
            if asset_type not in {"stock", "crypto"}:
                raise ValueError("نوع دارایی نامعتبر است.")
            flow["asset_type"] = asset_type
            if asset_type == "crypto":
                selected = [x for x in flow.get("analysts", "").split(",") if x and x != "fundamentals"]
                flow["analysts"] = ",".join(selected)
            backtest_confirm_screen(chat_id, cb_mid)

        elif data == "flow_cancel":
            FLOWS.pop(chat_id, None)
            home_screen(chat_id, target_mid=cb_mid)

        elif data == "analysis_today":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")
            flow["date"] = dt.date.today().isoformat()
            flow["stage"] = "analysts"
            analyst_picker(chat_id, backtest=False, target_mid=cb_mid)

        elif data == "analysis_change_date":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")
            flow["stage"] = "date"
            analysis_date_screen(chat_id, cb_mid)

        elif data == "analysis_analysts":
            analyst_picker(chat_id, backtest=False, target_mid=cb_mid)

        elif data == "backtest_today":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")

            end = dt.date.today().isoformat()
            if end < flow.get("start", end):
                raise ValueError("پایان قبل از شروع.")

            flow["end"] = end
            flow["stage"] = "every"
            backtest_every_screen(chat_id, cb_mid)

        elif data == "backtest_every":
            backtest_every_screen(chat_id, cb_mid)

        elif data == "backtest_analysts":
            analyst_picker(chat_id, backtest=True, target_mid=cb_mid)

        elif data.startswith("every:"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")
            flow["every"] = int(data.split(":", 1)[1])
            flow["stage"] = "analysts"
            analyst_picker(chat_id, backtest=True, target_mid=cb_mid)

        elif data.startswith("toggle_analyst:"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")

            key = data.split(":", 1)[1]
            selected = {x for x in flow.get("analysts", "").split(",") if x}

            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)

            flow["analysts"] = ",".join(x for x in ANALYST_ORDER if x in selected)
            analyst_picker(chat_id, backtest=flow.get("kind") == "backtest", target_mid=cb_mid)

        elif data == "analysts_done":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("منقضی.")
            if not flow.get("analysts"):
                raise ValueError("تحلیلگر انتخاب کن.")

            if flow.get("kind") == "analysis":
                analysis_confirm_screen(chat_id, cb_mid)
            else:
                backtest_confirm_screen(chat_id, cb_mid)

        elif data in ("analysis_run", "backtest_run"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("غیرفعال.")

            if flow.get("kind") == "analysis":
                params = {"ticker": flow["ticker"], "date": flow["date"], "analysts": flow.get("analysts", "")}
                mode = "analysis"
            else:
                params = {
                    "tickers": flow["tickers"],
                    "start": flow["start"],
                    "end": flow["end"],
                    "every": int(flow.get("every", 7)),
                    "asset_type": flow.get("asset_type", "stock"),
                    "analysts": flow.get("analysts", ""),
                }
                mode = "backtest"
            if flow.get("portfolio"):
                params["portfolio"] = flow["portfolio"]

            request_id = dispatch_run(chat_id, mode, params)
            FLOWS.pop(chat_id, None)

            label = "تحلیل" if mode == "analysis" else "بک‌تست"
            answer_callback(query["id"], f"🚀 {label} در صف!", True)

            _send_run_card(chat_id, mode, request_id, params)
            home_screen(chat_id)

        elif data == "noop":
            pass

    except Exception as exc:
        show(chat_id, f"<b>❌ خطا</b>\n{esc(exc)}", [[("بازگشت", "home")]], cb_mid)


def poll() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")

    me = tg("getMe")
    sys.stdout.write(f"TradingAgents controller started @{me.get('username', '')} build {BUILD}\n")
    sys.stdout.flush()

    threading.Thread(target=worker_loop, daemon=True).start()

    try:
        offset = int(get_setting("last_update_id", "0")) + 1
    except ValueError:
        offset = 1

    conflict_started = 0.0

    while True:
        try:
            updates = tg("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}) or []
            conflict_started = 0.0

            for update in updates:
                update_id = int(update["update_id"])
                offset = max(offset, update_id + 1)
                set_setting("last_update_id", str(update_id))

                try:
                    if "callback_query" in update:
                        callback(update["callback_query"])
                    elif "message" in update and update["message"].get("text"):
                        if authorized(int(update["message"]["from"]["id"])):
                            msg = update["message"]
                            handle_text(int(msg["chat"]["id"]), msg["text"], int(msg["message_id"]))
                except Exception:
                    sys.stderr.write(traceback.format_exc())

        except Exception as exc:
            message = str(exc)
            if "409" in message or "Conflict" in message:
                if not conflict_started:
                    conflict_started = time.monotonic()
                    sys.stdout.write("Another controller active; waiting...\n")
                    sys.stdout.flush()

                if time.monotonic() - conflict_started > 120:
                    sys.stdout.write("Still active; stopping.\n")
                    sys.stdout.flush()
                    return

                time.sleep(10)
            else:
                sys.stdout.write(f"Polling error: {message}\n")
                sys.stdout.flush()
                time.sleep(5)


def _install_shutdown_handler() -> None:
    """Fold the last writes into the state file when the runner stops us.

    GitHub Actions ends the job with SIGTERM. Without a handler the process
    dies right there and the writes that are still only in the write-ahead
    log are lost with the runner.
    """

    def handler(signum, _frame):
        sys.stdout.write(f"Received signal {signum}; saving controller state.\n")
        sys.stdout.flush()
        checkpoint_state()
        with contextlib.suppress(Exception):
            DB.close()
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            signal.signal(sig, handler)


if __name__ == "__main__":
    _install_shutdown_handler()
    poll()