#!/usr/bin/env python3
# ruff: noqa
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller - production architecture v18.

Core principles implemented:
- TradingAgents core behavior is preserved.
- RAW output is immutable and never cleaned, truncated, rewritten, or translated.
- Presentation is a separate read-only layer.
- Each request carries its own encrypted model/provider snapshot.
- Queue, recovery, ownership, and Telegram UX are handled by the controller.
"""

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

BUILD = "v18"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("BOT_GITHUB_TOKEN", "").strip()
STATE_KEY = os.getenv("BOT_STATE_KEY", "").strip()
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"
GITHUB_REF = os.getenv("GITHUB_REF", "main").strip() or "main"

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

TERMINAL = ("success", "failure", "cancelled", "stale")
ACTIVE_STATUSES = ("pending", "dispatching", "dispatched", "running")
DISPATCH_ACTIVE_STATUSES = ("dispatching", "dispatched", "running")

KNOWN_SUFFIXES = ("/chat/completions", "/completions", "/messages", "/generateContent")

MAX_TG_DOC_BYTES = 49_000_000
TEXT_CHUNK_SIZE = 3800
MAX_READ_PARTS = 50
CALLBACK_TOKEN_TTL_HOURS = 24
STATE_BACKUP_INTERVAL_SECONDS = 300

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

CACHE: dict[str, Any] = {"artifacts": [], "ts": 0.0}
WIZARDS: dict[int, dict[str, Any]] = {}
FLOWS: dict[int, dict[str, Any]] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def parse_iso(value: Any) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def crypt_key() -> bytes:
    seed = STATE_KEY or f"legacy:{OWNER}:{REPO}:{GH_TOKEN}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


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


def restore_state_from_repo() -> None:
    if os.path.exists(STATE_PATH):
        return
    if not GH_TOKEN:
        return

    try:
        headers = {
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "TradingAgentsArya-Bot",
        }
        path = urllib.parse.quote(".bot-state/bot_state.db.enc", safe="")
        url = f"{GH}/contents/{path}?ref={urllib.parse.quote(GITHUB_REF, safe='')}"
        status, obj, raw = http_json(url, "GET", headers=headers, timeout=30)
        if status == 200 and isinstance(obj, dict) and obj.get("content"):
            encrypted = base64.b64decode(obj.get("content", ""))
            from nacl.secret import SecretBox

            decrypted = SecretBox(crypt_key()).decrypt(encrypted)
            state_dir = os.path.dirname(os.path.abspath(STATE_PATH))
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            with open(STATE_PATH, "wb") as f:
                f.write(decrypted)
    except Exception:
        pass


restore_state_from_repo()

state_dir = os.path.dirname(os.path.abspath(STATE_PATH))
if state_dir:
    os.makedirs(state_dir, exist_ok=True)

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.execute("PRAGMA busy_timeout=5000")
with contextlib.suppress(Exception):
    DB.execute("PRAGMA journal_mode=WAL")

DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

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
        owner_user_id INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        model_id TEXT NOT NULL,
        model_snapshot_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        conclusion TEXT NOT NULL DEFAULT '',
        notified INTEGER NOT NULL DEFAULT 0,
        notify_attempts INTEGER NOT NULL DEFAULT 0,
        dispatch_attempts INTEGER NOT NULL DEFAULT 0,
        dispatched_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS callback_tokens (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );
    """
)

for stmt in (
    "ALTER TABLE runs ADD COLUMN workflow_run_id INTEGER",
    "ALTER TABLE runs ADD COLUMN owner_user_id INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN model_snapshot_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE runs ADD COLUMN conclusion TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN notify_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN dispatch_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN dispatched_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN finished_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN provider_id TEXT",
):
    with contextlib.suppress(sqlite3.OperationalError):
        DB.execute(stmt)

DB.commit()
DB_LOCK = threading.Lock()


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
            return cur
        except Exception as e:
            sys.stderr.write(f"DB Exec Error: {e}\nSQL: {sql}\nArgs: {args}\n")
            return None


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


def can_access_run(user_id: int, row: sqlite3.Row | None) -> bool:
    if not row:
        return False
    if admin(user_id):
        return True
    return int(row["owner_user_id"] or 0) == int(user_id)


def encrypt_bytes(data: bytes) -> bytes:
    from nacl.secret import SecretBox

    return bytes(SecretBox(crypt_key()).encrypt(data))


def decrypt_bytes(data: bytes) -> bytes:
    from nacl.secret import SecretBox

    return SecretBox(crypt_key()).decrypt(data)


def encrypt_token(token: str) -> bytes:
    return encrypt_bytes(token.encode("utf-8"))


def decrypt_token(ciphertext: Any) -> str:
    if not ciphertext:
        return ""
    try:
        return decrypt_bytes(bytes(ciphertext)).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("کلید رمزگشایی تغییر کرده؛ مدل/پرووایدر را دوباره ذخیره کن.") from exc


def encrypt_request_config(obj: dict[str, Any]) -> str:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(encrypt_bytes(payload)).decode("ascii")


def backup_state_to_repo(force: bool = False) -> None:
    try:
        if not GH_TOKEN:
            return
        if not os.path.exists(STATE_PATH):
            return

        last_ts = float(get_setting("last_backup_ts", "0") or "0")
        if not force and (time.time() - last_ts) < STATE_BACKUP_INTERVAL_SECONDS:
            return

        with DB_LOCK:
            with contextlib.suppress(Exception):
                DB.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with open(STATE_PATH, "rb") as f:
            data = f.read()

        encrypted = encrypt_bytes(data)
        content = base64.b64encode(encrypted).decode("ascii")
        path = ".bot-state/bot_state.db.enc"

        sha = None
        with contextlib.suppress(Exception):
            existing = gh("GET", f"/contents/{path}?ref={urllib.parse.quote(GITHUB_REF, safe='')}")
            if isinstance(existing, dict):
                sha = existing.get("sha")

        body = {
            "message": "chore: encrypted bot state backup",
            "content": content,
            "branch": GITHUB_REF,
        }
        if sha:
            body["sha"] = sha

        gh("PUT", f"/contents/{path}", body, timeout=60)
        set_setting("last_backup_ts", str(time.time()))
    except Exception:
        pass


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


def try_tg_document(chat_id: int, filename: str, data: bytes, caption: str) -> bool:
    if len(data) > MAX_TG_DOC_BYTES:
        return False
    try:
        tg_document(chat_id, filename, data, caption)
        return True
    except Exception:
        return False


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


def send_report_chunk(chat_id: int, text: str) -> None:
    tg(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
    )


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


def create_cb_token(user_id: int, chat_id: int, action: str, payload: dict[str, Any] | None = None) -> str:
    token = uuid.uuid4().hex[:16]
    db(
        "INSERT INTO callback_tokens(token,user_id,chat_id,action,payload_json,created_at) VALUES(?,?,?,?,?,?)",
        (token, int(user_id), int(chat_id), action, json.dumps(payload or {}, ensure_ascii=False), now()),
    )
    return f"cb:{token}"


def get_cb_token(token: str) -> Optional[sqlite3.Row]:
    row = q1("SELECT * FROM callback_tokens WHERE token=?", (token,))
    if not row:
        return None
    created = parse_iso(row["created_at"])
    if created:
        age = dt.datetime.now(dt.timezone.utc) - created
        if age > dt.timedelta(hours=CALLBACK_TOKEN_TTL_HOURS):
            return None
    return row


def cleanup_callback_tokens() -> None:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=CALLBACK_TOKEN_TTL_HOURS)).isoformat()
    db("DELETE FROM callback_tokens WHERE created_at < ?", (cutoff,))


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


def add_model_from_provider(prov_id: str, model_name: str) -> str:
    prov = get_provider(prov_id)
    if not prov:
        raise ValueError("Provider یافت نشد.")

    existing = q1("SELECT id FROM models WHERE name=? AND provider_id=? AND enabled=1", (model_name, prov_id))
    if existing:
        model_id = str(existing["id"])
        set_active_model(model_id)
        return model_id

    model_id = "m_" + uuid.uuid4().hex[:12]
    db(
        "INSERT INTO models(id,name,provider_id,base_url,token_ciphertext,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
        (model_id, model_name, prov_id, prov["base_url"], prov["token_ciphertext"], now()),
    )
    set_active_model(model_id)
    return model_id


def build_model_snapshot(model_id: str) -> dict[str, Any]:
    row = q1("SELECT * FROM models WHERE id=?", (model_id,))
    if not row:
        raise ValueError("مدل فعال پیدا نشد.")

    prov = get_provider(row["provider_id"])
    if not prov:
        raise ValueError("Provider مدل فعال پیدا نشد.")

    token_ciphertext = row["token_ciphertext"] or prov["token_ciphertext"]
    token_b64 = ""
    if token_ciphertext:
        token_b64 = base64.b64encode(bytes(token_ciphertext)).decode("ascii")

    return {
        "model_id": row["id"],
        "model_name": row["name"],
        "provider_id": prov["id"],
        "provider_name": prov["name"],
        "provider_type": prov["provider_type"],
        "base_url": strip_known_suffix(row["base_url"] or prov["base_url"]),
        "provider_env_key": PROVIDER_SECRET.get(prov["provider_type"], ""),
        "token_ciphertext_b64": token_b64,
    }


def create_request(chat_id: int, user_id: int, mode: str, params: dict[str, Any]) -> str:
    model_id = active_model_id()
    if not model_id:
        raise ValueError("اول از بخش مدل‌ها یک مدل فعال کن.")

    snapshot = build_model_snapshot(model_id)
    request_id = uuid.uuid4().hex

    db(
        "INSERT INTO runs("
        "request_id,workflow_run_id,chat_id,owner_user_id,mode,payload_json,model_id,model_snapshot_json,"
        "status,conclusion,notified,notify_attempts,dispatch_attempts,dispatched_at,finished_at,last_error,"
        "created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?, '', 0, 0, 0, '', '', '', ?, ?)",
        (
            request_id,
            None,
            int(chat_id),
            int(user_id),
            mode,
            json.dumps(params, ensure_ascii=False),
            model_id,
            json.dumps(snapshot, ensure_ascii=False),
            "pending",
            now(),
            now(),
        ),
    )

    backup_state_to_repo(force=True)
    return request_id


def fetch_repo_runs(limit: int = 100) -> list[dict[str, Any]]:
    data = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page={limit}", timeout=20) or {}
    return [r for r in data.get("workflow_runs", []) if r.get("event") == "repository_dispatch"]


def find_workflow_run_by_request_id(request_id: str, runs: list[dict[str, Any]] | None = None) -> Optional[dict[str, Any]]:
    if runs is None:
        runs = fetch_repo_runs()
    for run in runs:
        if str(request_id) in str(run.get("display_title") or ""):
            return run
    return None


def dispatch_pending() -> None:
    active = q1("SELECT request_id FROM runs WHERE status IN " + str(DISPATCH_ACTIVE_STATUSES) + " LIMIT 1")
    if active:
        return

    row = q1("SELECT * FROM runs WHERE status='pending' ORDER BY created_at ASC LIMIT 1")
    if not row:
        return

    cur = db(
        "UPDATE runs SET status='dispatching', updated_at=? WHERE request_id=? AND status='pending'",
        (now(), row["request_id"]),
    )
    if not cur or cur.rowcount == 0:
        return

    try:
        snapshot = json.loads(row["model_snapshot_json"] or "{}")
        if not snapshot.get("provider_type") or not snapshot.get("model_name") or not snapshot.get("base_url"):
            raise ValueError("Snapshot مدل کامل نیست.")

        token = ""
        if snapshot.get("token_ciphertext_b64"):
            token = decrypt_token(base64.b64decode(snapshot["token_ciphertext_b64"]))

        config = {
            "request_id": row["request_id"],
            "provider_type": snapshot["provider_type"],
            "model_name": snapshot["model_name"],
            "base_url": snapshot["base_url"],
            "provider_env_key": snapshot.get("provider_env_key", ""),
            "api_token": token,
        }

        payload = {
            "request_id": row["request_id"],
            "mode": row["mode"],
            "chat_id": str(row["chat_id"]),
            "user_id": str(row["owner_user_id"]),
            "model_id": row["model_id"],
            "params": json.loads(row["payload_json"] or "{}"),
            "config_enc": encrypt_request_config(config),
        }

        gh("POST", "/dispatches", {"event_type": "tradingagents_run", "client_payload": payload}, timeout=30)
        db(
            "UPDATE runs SET status='dispatched', dispatched_at=?, updated_at=?, last_error='' WHERE request_id=?",
            (now(), now(), row["request_id"]),
        )
    except Exception as exc:
        run = None
        with contextlib.suppress(Exception):
            run = find_workflow_run_by_request_id(row["request_id"])

        if run:
            db(
                "UPDATE runs SET status='dispatched', workflow_run_id=?, dispatched_at=?, updated_at=?, last_error='' WHERE request_id=?",
                (int(run.get("id") or 0), now(), now(), row["request_id"]),
            )
            return

        attempts = int(row["dispatch_attempts"] or 0) + 1
        new_status = "pending" if attempts < 3 else "failure"
        db(
            "UPDATE runs SET status=?, dispatch_attempts=?, last_error=?, updated_at=?, conclusion=CASE WHEN ?='failure' THEN 'dispatch_failed' ELSE conclusion END WHERE request_id=?",
            (new_status, attempts, str(exc)[:500], now(), new_status, row["request_id"]),
        )


def sync_run_records() -> None:
    rows = q("SELECT * FROM runs WHERE status IN " + str(DISPATCH_ACTIVE_STATUSES) + " ORDER BY updated_at DESC LIMIT 50")
    if not rows:
        return

    try:
        workflow_runs = fetch_repo_runs()
    except Exception:
        return

    for row in rows:
        run = find_workflow_run_by_request_id(row["request_id"], workflow_runs)
        if not run:
            continue

        status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        run_id = int(run.get("id") or 0)

        if status == "completed" and conclusion:
            final = conclusion if conclusion in TERMINAL else "failure"
            db(
                "UPDATE runs SET workflow_run_id=?, status=?, conclusion=?, finished_at=?, updated_at=? WHERE request_id=?",
                (run_id, final, conclusion, now(), now(), row["request_id"]),
            )
        elif status == "in_progress":
            db(
                "UPDATE runs SET workflow_run_id=?, status='running', conclusion='', updated_at=? WHERE request_id=?",
                (run_id, now(), row["request_id"]),
            )
        else:
            db(
                "UPDATE runs SET workflow_run_id=?, status='dispatched', conclusion='', updated_at=? WHERE request_id=?",
                (run_id, now(), row["request_id"]),
            )


def recover_stale_runs() -> None:
    rows = q("SELECT * FROM runs WHERE status IN " + str(DISPATCH_ACTIVE_STATUSES))
    if not rows:
        return

    now_dt = dt.datetime.now(dt.timezone.utc)

    for row in rows:
        updated = parse_iso(row["updated_at"]) or now_dt
        age = (now_dt - updated).total_seconds()

        if row["status"] == "dispatching" and age > 600:
            attempts = int(row["dispatch_attempts"] or 0) + 1
            new_status = "pending" if attempts < 3 else "failure"
            db(
                "UPDATE runs SET status=?, dispatch_attempts=?, last_error=?, updated_at=?, conclusion=CASE WHEN ?='failure' THEN 'dispatch_timeout' ELSE conclusion END WHERE request_id=?",
                (new_status, attempts, "dispatching timeout", now(), new_status, row["request_id"]),
            )

        elif row["status"] == "dispatched" and age > 1800:
            run = None
            with contextlib.suppress(Exception):
                run = find_workflow_run_by_request_id(row["request_id"])

            if run:
                continue

            attempts = int(row["dispatch_attempts"] or 0) + 1
            new_status = "pending" if attempts < 3 else "failure"
            db(
                "UPDATE runs SET status=?, dispatch_attempts=?, last_error=?, updated_at=?, conclusion=CASE WHEN ?='failure' THEN 'stale_dispatch' ELSE conclusion END WHERE request_id=?",
                (new_status, attempts, "dispatched but no workflow run found", now(), new_status, row["request_id"]),
            )

        elif row["status"] == "running" and age > (360 * 60 + 1800):
            db(
                "UPDATE runs SET status='stale', conclusion='stale_run', finished_at=?, updated_at=? WHERE request_id=?",
                (now(), now(), row["request_id"]),
            )


def notify_finished() -> None:
    rows = q("SELECT * FROM runs WHERE notified=0 AND status IN " + str(TERMINAL) + " ORDER BY updated_at LIMIT 10")
    for row in rows:
        if int(row["notify_attempts"] or 0) >= 5:
            db("UPDATE runs SET notified=1, updated_at=? WHERE request_id=?", (now(), row["request_id"]))
            continue

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
            "برای مشاهده گزارش کامل دکمه زیر را بزن."
        )

        buttons = [[("📄 گزارش کامل", f"view_output:{row['request_id']}")]]
        if row["workflow_run_id"]:
            buttons.append([("🔗 صفحه اجرا", f"url:https://github.com/{OWNER}/{REPO}/actions/runs/{row['workflow_run_id']}")])

        try:
            send_message(int(row["chat_id"]), text, buttons)
            db("UPDATE runs SET notified=1, updated_at=? WHERE request_id=?", (now(), row["request_id"]))
        except Exception:
            sys.stderr.write(traceback.format_exc())
            db(
                "UPDATE runs SET notify_attempts=notify_attempts+1, updated_at=? WHERE request_id=?",
                (now(), row["request_id"]),
            )


def fetch_active_runs() -> list[dict[str, Any]]:
    active = []
    for status in ("queued", "in_progress"):
        try:
            data = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=50", timeout=15) or {}
            for run in data.get("workflow_runs", []):
                if run.get("event") == "repository_dispatch":
                    active.append(run)
        except Exception:
            pass

    return list({int(run["id"]): run for run in active if run.get("id")}.values())


def cancel_run(run_id: int) -> None:
    try:
        gh("POST", f"/actions/runs/{int(run_id)}/cancel", timeout=15)
    except Exception:
        pass


def cancel_request_by_id(user_id: int, request_id: str) -> None:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row:
        raise ValueError("درخواست پیدا نشد.")
    if not can_access_run(user_id, row):
        raise PermissionError("دسترسی نداری.")

    if row["status"] == "pending":
        db(
            "UPDATE runs SET status='cancelled', conclusion='cancelled_by_user', finished_at=?, updated_at=? WHERE request_id=?",
            (now(), now(), request_id),
        )
        return

    if row["workflow_run_id"]:
        cancel_run(int(row["workflow_run_id"]))

    if row["status"] in ACTIVE_STATUSES:
        db(
            "UPDATE runs SET conclusion='cancel_requested', updated_at=? WHERE request_id=?",
            (now(), request_id),
        )


def hard_cancel(chat_id: int, user_id: int) -> str:
    WIZARDS.pop(chat_id, None)
    FLOWS.pop(chat_id, None)

    cancelled = 0

    if admin(user_id):
        try:
            for run in fetch_active_runs():
                cancel_run(int(run["id"]))
                cancelled += 1
        except Exception:
            pass
        rows = q("SELECT * FROM runs WHERE status IN " + str(ACTIVE_STATUSES))
    else:
        rows = q("SELECT * FROM runs WHERE owner_user_id=? AND status IN " + str(ACTIVE_STATUSES), (int(user_id),))

    for row in rows:
        if row["workflow_run_id"]:
            cancel_run(int(row["workflow_run_id"]))
            cancelled += 1

        if row["status"] == "pending":
            db(
                "UPDATE runs SET status='cancelled', conclusion='cancelled_by_user', finished_at=?, updated_at=? WHERE request_id=?",
                (now(), now(), row["request_id"]),
            )
        else:
            db(
                "UPDATE runs SET conclusion='cancel_requested', updated_at=? WHERE request_id=?",
                (now(), row["request_id"]),
            )

    return f"✅ توقف انجام شد ({cancelled} مورد)."


def worker_loop() -> None:
    last_backup = time.time()

    while True:
        for task in (
            dispatch_pending,
            sync_run_records,
            recover_stale_runs,
            lambda: CACHE.update(artifacts=(gh("GET", "/actions/artifacts?per_page=20", timeout=15) or {}).get("artifacts", [])),
            notify_finished,
            cleanup_callback_tokens,
        ):
            with contextlib.suppress(Exception):
                task()

        if time.time() - last_backup > STATE_BACKUP_INTERVAL_SECONDS:
            with contextlib.suppress(Exception):
                backup_state_to_repo()
            last_backup = time.time()

        CACHE["ts"] = time.monotonic()
        time.sleep(20)


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


def get_run_artifacts(run_id: int) -> list[dict[str, Any]]:
    data = gh("GET", f"/actions/runs/{int(run_id)}/artifacts", timeout=30) or {}
    return [a for a in data.get("artifacts", []) if not a.get("expired")]


def pick_artifact(artifacts: list[dict[str, Any]], prefixes: tuple[str, ...] | list[str]) -> Optional[dict[str, Any]]:
    for prefix in prefixes:
        for art in artifacts:
            if str(art.get("name", "")).startswith(prefix):
                return art
    return None


def choose_report_file(zf: zipfile.ZipFile) -> tuple[Optional[str], str]:
    scored = []

    for name in zf.namelist():
        if name.endswith("/"):
            continue

        low = name.lower()
        if not low.endswith((".md", ".txt", ".json")):
            continue

        try:
            body = zf.read(name).decode("utf-8", "replace")
        except Exception:
            continue

        score = 0
        if "full_report" in low:
            score += 10
        if "report" in low:
            score += 3
        if low.endswith(".md"):
            score += 2
        if "manifest" in low:
            score -= 5
        if "request.json" in low:
            score -= 3

        scored.append((score, name, body))

    if not scored:
        return None, ""

    scored.sort(key=lambda item: -item[0])
    return scored[0][1], scored[0][2]


def extract_summary(body: str) -> str:
    patterns = [
        r"(?i)(?:##?\s*?(?:Final Decision|Investment Plan|Portfolio Management|نتیجه نهایی|برنامه سرمایه‌گذاری)[^\n]*\n)([\s\S]{50,3500}?)(?=\n##|\Z)",
        r"(?i)(?:Action|Target Price|Stop Loss|Confidence|Reasoning|عمل|قیمت هدف|حد ضرر)[^\n]*\n([\s\S]{50,3500}?)(?=\n##|\Z)",
    ]

    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            snippet = match.group(1).strip()
            snippet = re.sub(r"```[\s\S]*?```", "", snippet).strip()
            if len(snippet) > 100:
                return snippet[:3500]

    clean = re.sub(r"```[\s\S]*?```", "", body).strip()
    return clean[:3500] if clean else "(خلاصه‌ای یافت نشد)"


def split_text(text: str, limit: int = TEXT_CHUNK_SIZE) -> list[str]:
    parts = []
    remaining = text or ""

    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break

        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        else:
            cut += 1

        parts.append(remaining[:cut])
        remaining = remaining[cut:]

    return parts


def send_long_text(chat_id: int, text: str, header: str) -> None:
    parts = split_text(text, TEXT_CHUNK_SIZE)
    if not parts:
        send_message(chat_id, "📭 محتوایی برای نمایش پیدا نشد.")
        return

    if len(parts) > MAX_READ_PARTS:
        send_message(
            chat_id,
            "⚠️ این گزارش برای ارسال مستقیم در تلگرام خیلی بزرگ است.\n"
            "هیچ بخشی حذف نمی‌شود؛ فایل کامل را ارسال می‌کنم.",
        )
        try_tg_document(chat_id, "report.txt", text.encode("utf-8"), header)
        return

    send_message(chat_id, f"{header}\n📖 این گزارش در {len(parts)} بخش ارسال می‌شود.")
    for part in parts:
        if part.strip():
            send_report_chunk(chat_id, part)
            time.sleep(0.35)


def send_zip_or_files(chat_id: int, zip_bytes: bytes, base_name: str) -> None:
    if try_tg_document(chat_id, f"{base_name}.zip", zip_bytes, "📦 فایل RAW کامل"):
        return

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        send_message(chat_id, "❌ فایل فشرده قابل خواندن نیست.")
        return

    sent_any = False
    too_large = []

    for name in zf.namelist():
        if name.endswith("/"):
            continue

        try:
            data = zf.read(name)
        except Exception:
            continue

        fname = name.replace("/", "__")
        if try_tg_document(chat_id, fname, data, f"📄 {fname}"):
            sent_any = True
            time.sleep(0.35)
        else:
            too_large.append(fname)

    if too_large:
        send_message(
            chat_id,
            "⚠️ برخی فایل‌ها از محدودیت ارسال تلگرام بزرگ‌تر هستند.\n"
            "این فایل‌ها در Artifact گیت‌هاب باقی می‌مانند:\n"
            f"<code>{esc(', '.join(too_large)[:3000])}</code>",
        )

    if not sent_any and not too_large:
        send_message(chat_id, "📭 فایلی برای ارسال پیدا نشد.")


def output_menu_thread(chat_id: int, user_id: int, request_id: str) -> None:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row or not can_access_run(user_id, row):
        send_message(chat_id, "❌ این اجرا پیدا نشد یا دسترسی نداری.", [[("بازگشت", "outputs")]])
        return

    if not row["workflow_run_id"]:
        with contextlib.suppress(Exception):
            sync_run_records()
        row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))

    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"
    label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"

    status_map = {
        "pending": "⏳ در صف",
        "dispatching": "⏳ در حال ارسال",
        "dispatched": "⏳ ارسال شده",
        "running": "🟡 در حال اجرا",
        "success": "✅ موفق",
        "failure": "❌ ناموفق",
        "cancelled": "🚫 لغو شده",
        "stale": "⚠️ نامشخص / قدیمی",
    }
    status_text = status_map.get(row["status"], row["status"])

    text = (
        f"<b>📄 خروجی {label}</b>\n"
        f"وضعیت: <b>{status_text}</b>\n"
        f"موضوع: <code>{esc(subject)}</code>\n"
        f"شناسه: <code>{esc(request_id[:12])}</code>\n\n"
        "یکی از گزینه‌های زیر را انتخاب کن."
    )

    summary_cb = create_cb_token(user_id, chat_id, "output_summary", {"request_id": request_id})
    read_cb = create_cb_token(user_id, chat_id, "output_read", {"request_id": request_id})
    raw_cb = create_cb_token(user_id, chat_id, "output_raw", {"request_id": request_id})
    log_cb = create_cb_token(user_id, chat_id, "output_log", {"request_id": request_id})

    rows = [
        [("🧾 خلاصه", summary_cb), ("📖 خواندن گزارش", read_cb)],
        [("📦 فایل RAW کامل", raw_cb), ("📋 لاگ اجرا", log_cb)],
        [("🏠 بازگشت", "outputs")],
    ]

    if row["workflow_run_id"]:
        rows.insert(2, [("🔗 صفحه اجرا", f"url:https://github.com/{OWNER}/{REPO}/actions/runs/{row['workflow_run_id']}")])

    send_message(chat_id, text, rows)


def output_action_thread(chat_id: int, user_id: int, request_id: str, action: str) -> None:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row or not can_access_run(user_id, row):
        send_message(chat_id, "❌ این اجرا پیدا نشد یا دسترسی نداری.")
        return

    if not row["workflow_run_id"]:
        with contextlib.suppress(Exception):
            sync_run_records()
        row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))

    run_id = row["workflow_run_id"]
    if not run_id:
        send_message(chat_id, "⏳ هنوز Run ID ثبت نشده است.")
        return

    try:
        artifacts = get_run_artifacts(int(run_id))
    except Exception as exc:
        send_message(chat_id, f"❌ دریافت لیست Artifact ناموفق: {esc(exc)}")
        return

    if action == "output_summary":
        art = pick_artifact(artifacts, ("tradingagents-presentation-", "tradingagents-results-", "tradingagents-raw-"))
        if not art:
            send_message(chat_id, "📭 هنوز Artifact خروجی ساخته نشده.")
            return

        try:
            zip_bytes = gh_bytes(f"/actions/artifacts/{art['id']}/zip")
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            name, body = choose_report_file(zf)
            summary = extract_summary(body) if body else "(خلاصه‌ای یافت نشد)"
            send_message(
                chat_id,
                f"<b>🧾 خلاصه گزارش</b>\n<code>{esc(name or 'report')}</code>\n\n{esc(summary)}",
            )
        except Exception as exc:
            send_message(chat_id, f"❌ دریافت خلاصه ناموفق: {esc(exc)}")
        return

    if action == "output_read":
        art = pick_artifact(artifacts, ("tradingagents-presentation-", "tradingagents-results-", "tradingagents-raw-"))
        if not art:
            send_message(chat_id, "📭 هنوز Artifact خروجی ساخته نشده.")
            return

        try:
            zip_bytes = gh_bytes(f"/actions/artifacts/{art['id']}/zip")
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            name, body = choose_report_file(zf)
            if not body:
                send_message(chat_id, "📭 گزارش متنی پیدا نشد.")
                return
            send_long_text(chat_id, body, f"📖 خواندن گزارش: <code>{esc(name)}</code>")
        except Exception as exc:
            send_message(chat_id, f"❌ خواندن گزارش ناموفق: {esc(exc)}")
        return

    if action == "output_raw":
        art = pick_artifact(artifacts, ("tradingagents-raw-", "tradingagents-results-", "tradingagents-run-"))
        if not art:
            send_message(chat_id, "📭 هنوز Artifact RAW ساخته نشده.")
            return

        try:
            zip_bytes = gh_bytes(f"/actions/artifacts/{art['id']}/zip")
            send_zip_or_files(chat_id, zip_bytes, f"raw_{request_id[:8]}")
        except Exception as exc:
            send_message(chat_id, f"❌ دریافت RAW ناموفق: {esc(exc)}")
        return

    if action == "output_log":
        art = pick_artifact(artifacts, ("tradingagents-run-", "tradingagents-raw-"))
        if not art:
            send_message(chat_id, "📭 لاگ اجرا پیدا نشد.")
            return

        try:
            zip_bytes = gh_bytes(f"/actions/artifacts/{art['id']}/zip")
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

            log_name = None
            for name in zf.namelist():
                if name.endswith("agent.log"):
                    log_name = name
                    break

            if log_name:
                data = zf.read(log_name)
                if not try_tg_document(chat_id, "agent.log", data, "📋 لاگ خام اجرا"):
                    send_message(chat_id, "⚠️ لاگ برای ارسال مستقیم از محدودیت تلگرام بزرگ‌تر است.")
            else:
                send_zip_or_files(chat_id, zip_bytes, f"log_{request_id[:8]}")
        except Exception as exc:
            send_message(chat_id, f"❌ دریافت لاگ ناموفق: {esc(exc)}")
        return


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


def home_screen(chat_id: int, user_id: int, force_new: bool = False, target_mid: int | None = None) -> int:
    aid = active_model_id()
    mrow = q1("SELECT name FROM models WHERE id=?", (aid,)) if aid else None
    model_line = f"مدل فعال: <b>{esc(mrow['name'])}</b>" if mrow else "مدل فعال: <b>تنظیم نشده</b>"

    if admin(user_id):
        busy_row = q1("SELECT COUNT(*) c FROM runs WHERE status IN " + str(ACTIVE_STATUSES))
    else:
        busy_row = q1(
            "SELECT COUNT(*) c FROM runs WHERE owner_user_id=? AND status IN " + str(ACTIVE_STATUSES),
            (int(user_id),),
        )
    busy = int(busy_row["c"]) if busy_row else 0
    live = f"\n🟡 درخواست فعال: <b>{busy}</b>" if busy else ""

    text = (
        f"<b>🤖 TradingAgentsArya</b>\n"
        f"{model_line}{live}\n\n"
        "به سیستم هوشمند تحلیل و مدیریت معاملات خوش آمدی.\n"
        f"<code>build {BUILD}</code>"
    )

    rows = [
        [("🚀 تحلیل جدید", "flow_analysis_start"), ("📈 بک‌تست", "flow_backtest_start")],
        [("🤖 اجراهای فعال", "active_runs"), ("📄 خروجی‌ها", "outputs")],
    ]

    if admin(user_id):
        rows.append([("🧠 مدل‌ها", "models"), ("📋 لاگ‌ها", "logs")])

    if force_new:
        new_mid = send_message(chat_id, text, rows)
        remember_ui(chat_id, new_mid)
        return new_mid

    return show(chat_id, text, rows, target_mid)


def models_screen(chat_id: int, page: int = 0, target_mid: int | None = None) -> int:
    rows = model_rows()
    aid = active_model_id()
    per_page = 4
    start = page * per_page
    end = start + per_page
    page_rows = rows[start:end]
    total_pages = max(1, (len(rows) + per_page - 1) // per_page)

    text = "<b>🧠 مدل‌ها</b>\nلیست مدل‌های هوش مصنوعی ثبت‌شده در سیستم.\n"
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
                btn_text = f"{mark}#{idx_num} {model_name[:28]}"
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
        [("🗑 حذف مدل", "model_delete_menu")],
        [("🏠 خانه", "home")],
    ]

    return show(chat_id, text, buttons, target_mid)


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
            row_buttons.append((f"🔹 {p['name'][:28]}", f"provider_detail:{p['id']}"))
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


def model_detail_screen(chat_id: int, user_id: int, prov_id: str, model_name: str, target_mid: int | None = None) -> int:
    test_cb = create_cb_token(user_id, chat_id, "provider_model_test", {"prov_id": prov_id, "model_name": model_name})
    select_cb = create_cb_token(user_id, chat_id, "provider_model_select", {"prov_id": prov_id, "model_name": model_name})

    return show(
        chat_id,
        f"<b>📋 جزئیات مدل</b>\nModel: <code>{esc(model_name)}</code>\nیک عملیات را انتخاب کن:",
        [
            [("🧪 تست اتصال", test_cb)],
            [("✅ انتخاب و فعال‌سازی", select_cb)],
            [("بازگشت", f"list_provider_models:{prov_id}")],
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
    buttons = [[(f"🧪 {r['name'][:32]}", f"test_model:{r['id']}")] for r in model_rows()]
    buttons.append([("بازگشت", "models")])
    return show(
        chat_id,
        "<b>🧪 تست اتصال</b>\nیک درخواست بسیار کوچک واقعی به API؛ TradingAgents اجرا نمی‌شود.",
        buttons,
        target_mid,
    )


def model_delete_menu(chat_id: int, target_mid: int | None = None) -> int:
    buttons = [[(f"🗑 {r['name'][:32]}", f"delete_model:{r['id']}")] for r in model_rows()]
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

    if not backtest and ticker and is_crypto(ticker):
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
        f"تحلیلگران: <b>{esc(chosen)}</b>",
        [
            [("🚀 شروع", "analysis_run")],
            [("🎛 تحلیلگران", "analysis_analysts"), ("📅 تاریخ", "analysis_change_date")],
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
        f"فاصله: <b>هر {esc(data.get('every', 7))} روز</b>",
        [
            [("🚀 شروع", "backtest_run")],
            [("🎛 تحلیلگران", "backtest_analysts"), ("📅 فاصله", "backtest_every")],
            [("❌ لغو", "flow_cancel")],
        ],
        target_mid,
    )


def active_runs_screen(chat_id: int, user_id: int, target_mid: int | None = None) -> int:
    if admin(user_id):
        rows = q("SELECT * FROM runs WHERE status IN " + str(ACTIVE_STATUSES) + " ORDER BY created_at DESC LIMIT 10")
    else:
        rows = q(
            "SELECT * FROM runs WHERE owner_user_id=? AND status IN " + str(ACTIVE_STATUSES) + " ORDER BY created_at DESC LIMIT 10",
            (int(user_id),),
        )

    lines = ["<b>🤖 اجراهای فعال</b>\nدرخواست‌هایی که در صف یا در حال اجرا هستند."]
    buttons = []

    if rows:
        lines.append("")
        for idx, row in enumerate(rows, 1):
            params = json.loads(row["payload_json"] or "{}")
            subject = params.get("ticker") or params.get("tickers") or "—"
            status_map = {
                "pending": "⏳ در صف",
                "dispatching": "⏳ در حال ارسال",
                "dispatched": "⏳ ارسال شده",
                "running": "🟡 در حال اجرا",
            }
            status_text = status_map.get(row["status"], row["status"])
            cancel_note = " | ⚠️ درخواست لغو شد" if row["conclusion"] == "cancel_requested" else ""
            lines.append(f"<b>#{idx}</b> {status_text} <code>{esc(subject)}</code>{cancel_note}")
            cancel_cb = create_cb_token(user_id, chat_id, "cancel_request", {"request_id": row["request_id"]})
            buttons.append([("🛑 لغو #" + str(idx), cancel_cb)])
            lines.append("")
    else:
        lines.append("\n✅ هیچ درخواست فعالی وجود ندارد.")

    buttons.append([("🔄 تازه‌سازی", "active_runs"), ("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def outputs_screen(chat_id: int, user_id: int, page: int = 0, target_mid: int | None = None) -> int:
    with contextlib.suppress(Exception):
        sync_run_records()

    per_page = 3
    offset = page * per_page

    if admin(user_id):
        rows = q("SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset))
        total_row = q1("SELECT COUNT(*) c FROM runs")
    else:
        rows = q(
            "SELECT * FROM runs WHERE owner_user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (int(user_id), per_page, offset),
        )
        total_row = q1("SELECT COUNT(*) c FROM runs WHERE owner_user_id=?", (int(user_id),))

    total_count = int(total_row["c"]) if total_row else 0
    total_pages = max(1, (total_count + per_page - 1) // per_page)

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
        if is_active:
            status_icon = "🟡"
        elif row["status"] == "success":
            status_icon = "✅"
        elif row["status"] == "cancelled":
            status_icon = "🚫"
        else:
            status_icon = "❌"

        lines.append(f"<b>#{idx}</b> {status_icon} <code>{esc(row['request_id'][:8])}</code> <b>{esc(subject)}</b> | {esc(created_str)}")

        btn_row = [("📄 گزارش #" + str(idx), f"view_output:{row['request_id']}")]
        if is_active:
            btn_row.append(("⚠️ در حال اجرا", "noop"))
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

    if admin(user_id):
        buttons.append([("🧹 پاک‌سازی", "bulk_delete_menu"), ("🔄 تازه‌سازی", "outputs_refresh")])
    else:
        buttons.append([("🔄 تازه‌سازی", "outputs_refresh")])

    buttons.append([("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def ask_delete_screen(chat_id: int, user_id: int, request_id: str, target_mid: int | None = None) -> int:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row or not can_access_run(user_id, row):
        return show(chat_id, "❌ پیدا نشد یا دسترسی نداری.", [[("بازگشت", "outputs")]], target_mid)

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


def logs_screen(chat_id: int, page: int = 0, target_mid: int | None = None) -> int:
    items = [a for a in CACHE.get("artifacts", []) if not a.get("expired")]
    run_logs = [i for i in items if str(i.get("name", "")).startswith("tradingagents-run-")]

    per_page = 3
    start = page * per_page
    end = start + per_page
    page_logs = run_logs[start:end]
    total_pages = max(1, (len(run_logs) + per_page - 1) // per_page)

    if not page_logs:
        return show(
            chat_id,
            "<b>📋 لاگ‌ها</b>\nهنوز فایل لاگی برای نمایش وجود ندارد.",
            [[("🏠 خانه", "home")]],
            target_mid,
        )

    lines = [
        f"<b>📋 لاگ‌ها</b> (صفحه {page + 1}/{total_pages})\nلیست فایل‌های لاگ خام اجراها.",
        "",
    ]
    buttons = []

    for idx, item in enumerate(page_logs, start=start + 1):
        try:
            created_str = dt.datetime.fromisoformat(str(item.get("created_at")).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_str = str(item.get("created_at"))[:16]

        lines.append(f"<b>#{idx}</b> — {esc(created_str)}")
        buttons.append([("📋 لاگ #" + str(idx), f"download_log:{item['id']}")])

    nav_row = []
    if page > 0:
        nav_row.append(("◀️ قبلی", f"logs_page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(("بعدی ▶️", f"logs_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([("🔄 تازه‌سازی", "logs_refresh"), ("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons, target_mid)


def handle_text(chat_id: int, user_id: int, text: str, target_mid: int) -> None:
    text = text.strip()

    if text.lower() == "/cancel":
        result = hard_cancel(chat_id, user_id)
        send_message(chat_id, result, [[("🏠 خانه", "home")]])
        return

    if text.startswith("/start"):
        WIZARDS.pop(chat_id, None)
        FLOWS.pop(chat_id, None)
        home_screen(chat_id, user_id, force_new=True)
        return

    if chat_id in WIZARDS:
        if not admin(user_id):
            WIZARDS.pop(chat_id, None)
            home_screen(chat_id, user_id, target_mid=target_mid)
            return

        item = WIZARDS[chat_id]
        stage = item.get("stage")

        try:
            if stage == "url":
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
                answer_callback(f"temp_{chat_id}", "✅ Provider ذخیره شد!", True)
                wizard_model_manual_screen(chat_id, target_mid)

            elif stage == "model_manual":
                if not text:
                    raise ValueError("Model ID خالی.")

                prov_id = item.get("prov_id")
                if not prov_id:
                    raise ValueError("Provider یافت نشد.")

                model_id = add_model_from_provider(prov_id, text)
                WIZARDS.pop(chat_id, None)
                answer_callback(f"temp_{chat_id}", "✅ مدل ذخیره شد!", True)

                show(
                    chat_id,
                    f"<b>✅ مدل ذخیره و فعال شد</b>\n<code>{esc(text)}</code>",
                    [[("🧪 تست", f"test_model:{model_id}"), ("🏠 خانه", "home")]],
                    target_mid,
                )

        except ValueError as exc:
            show(chat_id, f"<b>⚠️ {esc(exc)}</b>\nلطفاً دوباره تلاش کن.", [[("❌ لغو", "wizard_cancel")]], target_mid)

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

        except ValueError as exc:
            show(chat_id, f"<b>⚠️ {esc(exc)}</b>\nلطفاً دوباره تلاش کن.", [[("❌ لغو", "flow_cancel")]], target_mid)


def handle_token_action(
    chat_id: int,
    user_id: int,
    token_row: sqlite3.Row,
    payload: dict[str, Any],
    query_id: str,
    target_mid: int,
) -> None:
    action = str(token_row["action"])

    if action == "provider_model_test":
        if not admin(user_id):
            raise PermissionError("فقط ادمین.")

        prov = get_provider(str(payload.get("prov_id", "")))
        if not prov:
            raise ValueError("Provider یافت نشد.")

        model_name = str(payload.get("model_name", ""))
        token = decrypt_token(prov["token_ciphertext"])
        answer_callback(query_id, "⏳ در حال تست...", False)
        threading.Thread(
            target=run_model_test_async,
            args=(chat_id, model_name, prov["base_url"], token, prov["provider_type"], f"list_provider_models:{prov['id']}"),
            daemon=True,
        ).start()
        return

    if action == "provider_model_select":
        if not admin(user_id):
            raise PermissionError("فقط ادمین.")

        model_id = add_model_from_provider(str(payload.get("prov_id", "")), str(payload.get("model_name", "")))
        answer_callback(query_id, "✅ مدل فعال شد!", True)
        models_screen(chat_id, page=0, target_mid=target_mid)
        return

    if action == "cancel_request":
        request_id = str(payload.get("request_id", ""))
        cancel_request_by_id(user_id, request_id)
        answer_callback(query_id, "🛑 درخواست برای لغو ارسال شد.", True)
        active_runs_screen(chat_id, user_id, target_mid=target_mid)
        return

    if action in ("output_summary", "output_read", "output_raw", "output_log"):
        request_id = str(payload.get("request_id", ""))
        answer_callback(query_id, "⏳ در حال آماده‌سازی...", False)
        threading.Thread(target=output_action_thread, args=(chat_id, user_id, request_id, action), daemon=True).start()
        return

    answer_callback(query_id, "عملیات ناشناخته.", True)


def callback(query: dict[str, Any]) -> None:
    user_id = int(query.get("from", {}).get("id", 0))
    if not authorized(user_id):
        answer_callback(query["id"], "دسترسی مجاز نیست.", True)
        return

    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    cb_mid = int(message.get("message_id", 0))
    data = query.get("data", "")

    if data.startswith("cb:"):
        token_row = get_cb_token(data[3:])
        if not token_row:
            answer_callback(query["id"], "دکمه منقضی شده. دوباره منو را باز کن.", True)
            return

        if int(token_row["user_id"]) != user_id and not admin(user_id):
            answer_callback(query["id"], "دسترسی نداری.", True)
            return

        payload = json.loads(token_row["payload_json"] or "{}")
        try:
            handle_token_action(chat_id, user_id, token_row, payload, query["id"], cb_mid)
        except Exception as exc:
            answer_callback(query["id"], str(exc)[:200], True)
        return

    if data.startswith("view_output:"):
        answer_callback(query["id"], "📄 در حال آماده‌سازی...", False)
        threading.Thread(target=output_menu_thread, args=(chat_id, user_id, data.split(":", 1)[1]), daemon=True).start()
        return

    if data.startswith("download_log:"):
        answer_callback(query["id"], "📋 در حال دانلود...", False)
    elif data == "fetch_models_list":
        answer_callback(query["id"], "📋 در حال دریافت...", False)
    else:
        answer_callback(query["id"])

    try:
        if data == "home":
            WIZARDS.pop(chat_id, None)
            FLOWS.pop(chat_id, None)
            home_screen(chat_id, user_id, target_mid=cb_mid)

        elif data == "models":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("models_page:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            page = int(data.split(":", 1)[1])
            models_screen(chat_id, page=page, target_mid=cb_mid)

        elif data == "providers_menu":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            providers_menu(chat_id, cb_mid)

        elif data.startswith("provider_detail:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            provider_detail_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("ask_delete_provider:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            ask_delete_provider_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("confirm_delete_provider:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            delete_provider(data.split(":", 1)[1])
            answer_callback(query["id"], "🗑 Provider حذف شد.", True)
            providers_menu(chat_id, cb_mid)

        elif data.startswith("list_provider_models:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            list_provider_models_screen(chat_id, data.split(":", 1)[1], page=0, target_mid=cb_mid)

        elif data.startswith("prov_models_page:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            parts = data.split(":")
            list_provider_models_screen(chat_id, parts[1], page=int(parts[2]), target_mid=cb_mid)

        elif data.startswith("md:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            parts = data.split(":")
            prov_id = parts[1]
            idx = int(parts[2])

            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد (لیست منقضی شده).")

            model_name = models[idx]
            model_detail_screen(chat_id, user_id, prov_id, model_name, cb_mid)

        elif data.startswith("md_temp:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            idx = int(data.split(":", 1)[1])
            wizard = WIZARDS.get(chat_id, {})
            models = wizard.get("models_list", [])
            prov_id = wizard.get("prov_id", "")

            if idx < 0 or idx >= len(models):
                raise ValueError("مدل یافت نشد.")

            model_name = models[idx]
            model_detail_screen(chat_id, user_id, prov_id, model_name, cb_mid)

        elif data.startswith("manual_model_for_prov:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

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
                raise PermissionError("فقط ادمین.")
            WIZARDS[chat_id] = {"stage": "url"}
            wizard_url_screen(chat_id, cb_mid)

        elif data == "wizard_cancel":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            WIZARDS.pop(chat_id, None)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "wizard_manual_model":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            WIZARDS[chat_id]["stage"] = "model_manual"
            wizard_model_manual_screen(chat_id, cb_mid)

        elif data == "fetch_models_list":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

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
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            page = int(data.split(":", 1)[1])
            models_list_screen(chat_id, page=page, target_mid=cb_mid)

        elif data.startswith("test_model:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

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
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (data.split(":", 1)[1],))
            if not row:
                raise ValueError("مدل پیدا نشد.")

            set_active_model(row["id"])
            answer_callback(query["id"], "⚡ مدل فعال شد!", True)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "model_delete_menu":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            model_delete_menu(chat_id, cb_mid)

        elif data.startswith("delete_model:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            model_id = data.split(":", 1)[1]
            db("UPDATE models SET enabled=0 WHERE id=?", (model_id,))
            if active_model_id() == model_id:
                set_active_model("")

            answer_callback(query["id"], "🗑 حذف شد.", True)
            models_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "active_runs":
            active_runs_screen(chat_id, user_id, cb_mid)

        elif data == "outputs":
            outputs_screen(chat_id, user_id, page=0, target_mid=cb_mid)

        elif data == "outputs_refresh":
            outputs_screen(chat_id, user_id, page=0, target_mid=cb_mid)

        elif data.startswith("outputs_page:"):
            page = int(data.split(":", 1)[1])
            outputs_screen(chat_id, user_id, page=page, target_mid=cb_mid)

        elif data.startswith("ask_delete:"):
            ask_delete_screen(chat_id, user_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("confirm_delete:"):
            request_id = data.split(":", 1)[1]
            row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
            if not row or not can_access_run(user_id, row):
                raise PermissionError("دسترسی نداری.")

            db("DELETE FROM runs WHERE request_id=?", (request_id,))
            answer_callback(query["id"], "✅ حذف شد.", True)
            outputs_screen(chat_id, user_id, page=0, target_mid=cb_mid)

        elif data == "bulk_delete_menu":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            bulk_delete_menu(chat_id, cb_mid)

        elif data.startswith("bulk_scope:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            bulk_confirm_screen(chat_id, data.split(":", 1)[1], cb_mid)

        elif data.startswith("bulk_do:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            scope = data.split(":", 1)[1]
            if scope == "all":
                cur = db("DELETE FROM runs")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(scope))).isoformat()
                cur = db("DELETE FROM runs WHERE created_at < ?", (cutoff,))

            answer_callback(query["id"], f"✅ {cur.rowcount if cur else 0} حذف شد.", True)
            outputs_screen(chat_id, user_id, page=0, target_mid=cb_mid)

        elif data == "logs":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            logs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data == "logs_refresh":
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            logs_screen(chat_id, page=0, target_mid=cb_mid)

        elif data.startswith("logs_page:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")
            page = int(data.split(":", 1)[1])
            logs_screen(chat_id, page=page, target_mid=cb_mid)

        elif data.startswith("download_log:"):
            if not admin(user_id):
                raise PermissionError("فقط ادمین.")

            artifact_id = data.split(":", 1)[1]

            def download_log_admin():
                try:
                    raw = gh_bytes(f"/actions/artifacts/{artifact_id}/zip")
                except Exception as exc:
                    send_message(chat_id, f"❌ دانلود لاگ ناموفق: {esc(exc)}")
                    return

                zf = zipfile.ZipFile(io.BytesIO(raw))
                log_name = None
                for name in zf.namelist():
                    if name.endswith("agent.log"):
                        log_name = name
                        break

                if not log_name:
                    send_message(chat_id, "📭 فایل لاگ پیدا نشد.")
                    return

                data = zf.read(log_name)
                if not try_tg_document(chat_id, "agent.log", data, "📋 لاگ خام اجرا"):
                    send_message(chat_id, "⚠️ لاگ برای ارسال مستقیم از محدودیت تلگرام بزرگ‌تر است.")

            threading.Thread(target=download_log_admin, daemon=True).start()

        elif data == "flow_analysis_start":
            if not active_model_id():
                raise ValueError("اول مدل فعال کن.")
            FLOWS[chat_id] = {"kind": "analysis", "stage": "ticker", "analysts": "market,social,news,fundamentals"}
            analysis_ticker_screen(chat_id, cb_mid)

        elif data == "flow_backtest_start":
            if not active_model_id():
                raise ValueError("اول مدل فعال کن.")
            FLOWS[chat_id] = {"kind": "backtest", "stage": "tickers", "every": 7, "analysts": "market,social,news,fundamentals"}
            backtest_tickers_screen(chat_id, cb_mid)

        elif data == "flow_cancel":
            FLOWS.pop(chat_id, None)
            home_screen(chat_id, user_id, target_mid=cb_mid)

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
                    "analysts": flow.get("analysts", ""),
                }
                mode = "backtest"

            request_id = create_request(chat_id, user_id, mode, params)
            FLOWS.pop(chat_id, None)

            label = "تحلیل" if mode == "analysis" else "بک‌تست"
            answer_callback(query["id"], f"🚀 {label} در صف قرار گرفت!", True)

            show(
                chat_id,
                f"<b>🚀 در صف</b>\n<code>{esc(request_id[:10])}</code>\nدرخواست تو بدون حذف در صف پردازش قرار گرفت.",
                [[("🤖 اجراها", "active_runs"), ("🏠 خانه", "home")]],
                cb_mid,
            )

        elif data == "noop":
            pass

    except PermissionError as exc:
        answer_callback(query["id"], str(exc)[:200], True)
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

                try:
                    if "callback_query" in update:
                        callback(update["callback_query"])
                    elif "message" in update and update["message"].get("text"):
                        msg = update["message"]
                        user_id = int(msg["from"]["id"])
                        if authorized(user_id):
                            handle_text(int(msg["chat"]["id"]), user_id, msg["text"], int(msg["message_id"]))
                except Exception:
                    sys.stderr.write(traceback.format_exc())
                finally:
                    set_setting("last_update_id", str(update_id))

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


if __name__ == "__main__":
    poll()