#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller.

Design goals:
- one Telegram controller instance
- one editable UI message per chat
- guided flows; no command syntax is required
- model setup asks only URL -> token -> exact model id
- model test is a real, tiny API connectivity test (not a TradingAgents run)
- GitHub Actions is the execution layer
- persistent controller state lives in an Actions artifact
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import os
import sqlite3
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("BOT_GITHUB_TOKEN", "").strip()
STATE_KEY = os.getenv("BOT_STATE_KEY", "").strip()
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
REF = os.getenv("GITHUB_REF", "main").strip()
STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"
ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_IDS = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH = f"https://api.github.com/repos/{OWNER}/{REPO}"
WORKFLOW = ".github/workflows/tradingagents.yml"

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
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
    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        base_url TEXT NOT NULL,
        token_ciphertext BLOB,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        request_id TEXT PRIMARY KEY,
        workflow_run_id INTEGER,
        chat_id INTEGER NOT NULL,
        mode TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        model_id TEXT NOT NULL,
        status TEXT NOT NULL,
        conclusion TEXT DEFAULT '',
        fallback_attempted INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
)
DB.commit()

# In-memory only: API tokens are never written to the controller DB while a
# model-creation wizard is in progress. Once saved, the token is encrypted.
WIZARDS: dict[int, dict[str, str]] = {}
FLOWS: dict[int, dict[str, Any]] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def db(sql: str, args: tuple | list = ()):
    cur = DB.execute(sql, args)
    DB.commit()
    return cur


def get_setting(key: str, default: str = "") -> str:
    row = DB.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
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


# ---------- HTTP ----------
def http_json(url: str, method: str = "GET", data: Any = None, headers: dict | None = None, timeout: int = 60):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request_headers = {"Accept": "application/vnd.github+json"}
    if headers:
        request_headers.update(headers)
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
        return response.status, (json.loads(raw) if raw else None)


def http_error_code(exc: Exception) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return int(exc.code)
    return None


def tg(method: str, payload: dict | None = None):
    try:
        _, obj = http_json(
            f"{TG}/{method}",
            "POST",
            payload or {},
            {"Content-Type": "application/json"},
            timeout=65,
        )
    except Exception as exc:
        raise RuntimeError(f"Telegram API {method} failed") from exc
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed")
    return obj.get("result")


def gh(method: str, path: str, data: Any = None):
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TradingAgentsArya-Telegram",
    }
    try:
        status, obj = http_json(GH + path, method, data, headers, timeout=60)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API error {exc.code}") from exc
    if status >= 300:
        raise RuntimeError(f"GitHub API error {status}")
    return obj


# ---------- Telegram UI ----------
def inline(rows: list[list[tuple[str, str]]]) -> list[list[dict[str, str]]]:
    return [[{"text": text, "callback_data": data} for text, data in row] for row in rows]


def answer_callback(query_id: str, text: str = "", alert: bool = False) -> None:
    tg(
        "answerCallbackQuery",
        {"callback_query_id": query_id, "text": text[:200], "show_alert": alert},
    )


def send_message(chat_id: int, text: str, rows: list[list[tuple[str, str]]] | None = None) -> int:
    result = tg(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            **({"reply_markup": {"inline_keyboard": inline(rows)}} if rows is not None else {}),
        },
    )
    return int(result["message_id"])


def edit_message(chat_id: int, message_id: int, text: str, rows: list[list[tuple[str, str]]] | None = None) -> bool:
    try:
        tg(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                **({"reply_markup": {"inline_keyboard": inline(rows)}} if rows is not None else {}),
            },
        )
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False


def remember_ui(chat_id: int, message_id: int) -> None:
    db(
        "INSERT INTO ui(chat_id,message_id,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id,updated_at=excluded.updated_at",
        (chat_id, message_id, now()),
    )


def ui_message(chat_id: int) -> int | None:
    row = DB.execute("SELECT message_id FROM ui WHERE chat_id=?", (chat_id,)).fetchone()
    return int(row[0]) if row else None


def show(chat_id: int, text: str, rows: list[list[tuple[str, str]]], message_id: int | None = None) -> int:
    mid = message_id or ui_message(chat_id)
    if mid and edit_message(chat_id, mid, text, rows):
        remember_ui(chat_id, mid)
        return mid
    new_mid = send_message(chat_id, text, rows)
    remember_ui(chat_id, new_mid)
    return new_mid


# ---------- Model storage ----------
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
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "ollama": "",
}


def crypt_key() -> bytes:
    # BOT_STATE_KEY is stable across PAT rotations when configured. The PAT hash
    # remains a backward-compatible fallback for old controller state.
    seed = STATE_KEY or f"legacy:{OWNER}:{REPO}:{GH_TOKEN}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def encrypt_token(token: str) -> bytes:
    from nacl.secret import SecretBox
    return bytes(SecretBox(crypt_key()).encrypt(token.encode("utf-8")))


def decrypt_token(ciphertext: bytes | memoryview | None) -> str:
    if not ciphertext:
        return ""
    from nacl.secret import SecretBox
    try:
        return SecretBox(crypt_key()).decrypt(bytes(ciphertext)).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("کلید رمزگشایی State تغییر کرده است؛ مدل را دوباره اضافه کن.") from exc


def normalize_base_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("Base URL باید با http:// یا https:// شروع شود.")
    value = value.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/messages", "/generateContent"):
        if value.lower().endswith(suffix.lower()):
            value = value[: -len(suffix)].rstrip("/")
    if not value:
        raise ValueError("Base URL معتبر نیست.")
    return value


def infer_provider(base_url: str) -> str:
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


def model_rows() -> list[sqlite3.Row]:
    return DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()


def active_model_id() -> str:
    return get_setting("active_model")


def set_active_model(model_id: str) -> None:
    row = DB.execute("SELECT id FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
    if not row:
        raise ValueError("مدل پیدا نشد.")
    set_setting("active_model", model_id)


def github_secret(name: str, value: str) -> None:
    from nacl import encoding, public

    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode("utf-8"))
    gh(
        "PUT",
        f"/actions/secrets/{urllib.parse.quote(name, safe='')}",
        {
            "encrypted_value": base64.b64encode(encrypted).decode("ascii"),
            "key_id": key["key_id"],
        },
    )


def write_active_model_secrets(model_row: sqlite3.Row) -> None:
    provider = model_row["provider"]
    token = decrypt_token(model_row["token_ciphertext"])
    if provider in PROVIDER_SECRET and token:
        github_secret(PROVIDER_SECRET[provider], token)
    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", model_row["name"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", model_row["name"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", model_row["base_url"])
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")


def add_model(name: str, provider: str, base_url: str, token: str) -> str:
    model_id = "m_" + uuid.uuid4().hex[:12]
    timestamp = now()
    db(
        "INSERT INTO models(id,name,provider,base_url,token_ciphertext,enabled,created_at,updated_at) "
        "VALUES(?,?,?,?,?,1,?,?)",
        (model_id, name, provider, base_url, encrypt_token(token), timestamp, timestamp),
    )
    row = DB.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
    if row is None:
        raise RuntimeError("ذخیره مدل انجام نشد.")
    write_active_model_secrets(row)
    set_active_model(model_id)
    return model_id


# ---------- Real model test ----------
def post_model_test(model: sqlite3.Row) -> float:
    provider = model["provider"]
    base_url = model["base_url"].rstrip("/")
    token = decrypt_token(model["token_ciphertext"])
    model_name = model["name"]
    started = time.monotonic()

    if provider == "anthropic":
        endpoint = base_url if base_url.endswith("/messages") else base_url + "/v1/messages"
        status, _ = http_json(
            endpoint,
            "POST",
            {"model": model_name, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK only."}]},
            {"x-api-key": token, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            timeout=45,
        )
    elif provider == "google":
        encoded_model = urllib.parse.quote(model_name, safe="")
        endpoint = base_url + f"/models/{encoded_model}:generateContent"
        endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode({"key": token})
        status, _ = http_json(
            endpoint,
            "POST",
            {"contents": [{"parts": [{"text": "Reply OK only."}]}], "generationConfig": {"maxOutputTokens": 8}},
            {"Content-Type": "application/json"},
            timeout=45,
        )
    else:
        endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        status, _ = http_json(
            endpoint,
            "POST",
            {"model": model_name, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8},
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"} if token else {"Content-Type": "application/json"},
            timeout=45,
        )

    if status < 200 or status >= 300:
        raise RuntimeError(f"HTTP {status}")
    return time.monotonic() - started


# ---------- GitHub execution ----------
def current_engine_runs() -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for status in ("queued", "in_progress"):
        data = gh("GET", f"/actions/workflows/tradingagents.yml/runs?status={status}&per_page=50") or {}
        for run in data.get("workflow_runs", []):
            # Normal Telegram execution uses repository_dispatch. Manual
            # workflow_dispatch is reserved for the Controller only.
            if run.get("event") == "repository_dispatch":
                active.append(run)
    unique = {int(run["id"]): run for run in active if run.get("id")}
    return list(unique.values())


def dispatch_run(chat_id: int, mode: str, params: dict[str, Any]) -> tuple[str, str]:
    model_id = active_model_id()
    if not model_id:
        raise ValueError("اول از بخش مدل‌ها یک مدل فعال کن.")

    active = current_engine_runs()
    if active:
        raise ValueError("یک پردازش در حال اجراست. بعد از پایان آن، اجرای بعدی را شروع کن.")

    request_id = uuid.uuid4().hex
    payload = {
        "request_id": request_id,
        "mode": mode,
        "chat_id": str(chat_id),
        "model_id": model_id,
        "params": params,
    }

    # One request is sent exactly once. We deliberately do not fall back to a
    # second trigger because that could start the engine twice.
    gh("POST", "/dispatches", {"event_type": "tradingagents_run", "client_payload": payload})

    db(
        "INSERT INTO runs(request_id,workflow_run_id,chat_id,mode,payload_json,model_id,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?, ?,?)",
        (
            request_id,
            None,
            chat_id,
            mode,
            json.dumps(params, ensure_ascii=False),
            model_id,
            "queued",
            now(),
            now(),
        ),
    )
    return request_id, model_id


def sync_run_records() -> None:
    rows = DB.execute(
        "SELECT * FROM runs WHERE status NOT IN ('completed','failure','cancelled','skipped') "
        "ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    if not rows:
        return

    data = gh("GET", "/actions/workflows/tradingagents.yml/runs?per_page=100") or {}
    workflow_runs = [r for r in data.get("workflow_runs", []) if r.get("event") == "repository_dispatch"]
    by_request: dict[str, dict[str, Any]] = {}
    for run in workflow_runs:
        title = str(run.get("display_title") or "")
        for row in rows:
            if row["request_id"] in title:
                by_request[row["request_id"]] = run
                break

    for row in rows:
        run = by_request.get(row["request_id"])
        if not run:
            continue
        status = str(run.get("status") or "queued")
        conclusion = str(run.get("conclusion") or "")
        state = conclusion if status == "completed" and conclusion else status
        db(
            "UPDATE runs SET workflow_run_id=?,status=?,conclusion=?,updated_at=? WHERE request_id=?",
            (run.get("id"), state, conclusion, now(), row["request_id"]),
        )


def engine_link(run_id: int | None) -> str:
    return f"https://github.com/{OWNER}/{REPO}/actions/runs/{run_id}" if run_id else ""


# ---------- Text validation ----------
def valid_date(value: str) -> str:
    value = value.strip().lower()
    if value in ("today", "امروز"):
        return dt.date.today().isoformat()
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("تاریخ را مثل 2026-09-30 وارد کن.") from exc
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
                raise ValueError(f"Ticker نامعتبر است: {part}")
            if part not in values:
                values.append(part)
        return ",".join(values)
    if not raw or any(ch.isspace() for ch in raw):
        raise ValueError("فقط یک Ticker وارد کن؛ مثلاً NVDA")
    if not all(ch.isalnum() or ch in "._-^=" for ch in raw) or len(raw) > 32:
        raise ValueError("فرمت Ticker نامعتبر است.")
    return raw


def is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith(("-USD", "-USDT", "-USDC", "-BTC", "-ETH"))


# ---------- Main screens ----------
def home_text() -> str:
    aid = active_model_id()
    row = DB.execute("SELECT name,provider FROM models WHERE id=?", (aid,)).fetchone() if aid else None
    model_line = f"مدل فعال: <b>{esc(row['name'])}</b>" if row else "مدل فعال: <b>تنظیم نشده</b>"
    return f"<b>🤖 TradingAgentsArya</b>\n\n{model_line}\n\nیک گزینه را انتخاب کن؛ بقیه مراحل مرحله‌به‌مرحله از خودت سؤال می‌شوند."


def home(chat_id: int, message_id: int | None = None) -> int:
    return show(
        chat_id,
        home_text(),
        [
            [("🚀 اجرای جدید", "run") , ("🤖 مدل‌ها", "models")],
            [("📊 اجراهای جاری", "active_runs"), ("📜 تاریخچه", "history")],
            [("📦 خروجی‌ها", "artifacts")],
        ],
        message_id,
    )


def model_screen(chat_id: int, message_id: int | None = None) -> int:
    rows = model_rows()
    aid = active_model_id()
    text = "<b>🤖 مدل‌ها</b>\n\n"
    buttons: list[list[tuple[str, str]]] = []
    if not rows:
        text += "هنوز مدلی ثبت نشده."
    else:
        for row in rows:
            mark = "✅ " if row["id"] == aid else ""
            text += f"{mark}<b>{esc(row['name'])}</b>\n<code>{esc(row['provider'])}</code>\n\n"
            buttons.append([(f"⚡ فعال‌سازی {row['name']}", f"activate:{row['id']}")])
    buttons += [
        [("➕ افزودن مدل", "model_add"), ("🧪 تست اتصال", "model_test_menu")],
        [("🗑 حذف مدل", "model_delete_menu")],
        [("🏠 خانه", "home")],
    ]
    return show(chat_id, text.rstrip(), buttons, message_id)


def run_menu(chat_id: int, message_id: int | None = None) -> int:
    return show(
        chat_id,
        "<b>🚀 اجرای جدید</b>\n\nنوع کار را انتخاب کن:",
        [[("🔎 تحلیل یک Ticker", "flow_analysis_start")], [("📈 بک‌تست", "flow_backtest_start")], [("🏠 خانه", "home")]],
        message_id,
    )


def active_runs_screen(chat_id: int, message_id: int | None = None) -> int:
    try:
        runs = current_engine_runs()
    except Exception:
        runs = []
    if not runs:
        text = "<b>📊 اجراهای جاری</b>\n\n✅ هیچ پردازش Engine در حال اجرا نیست."
    else:
        lines = ["<b>📊 اجراهای جاری</b>", ""]
        for run in sorted(runs, key=lambda x: x.get("created_at", ""), reverse=True):
            title = esc(run.get("display_title") or "Engine")
            lines.append(f"🟡 <b>{title}</b>")
            lines.append(f"Run: <code>{esc(run.get('id'))}</code>")
        text = "\n".join(lines)
    return show(chat_id, text, [[("🔄 تازه‌سازی", "active_runs"), ("🏠 خانه", "home")]], message_id)


def history_screen(chat_id: int, message_id: int | None = None) -> int:
    try:
        sync_run_records()
    except Exception:
        pass
    rows = DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 12").fetchall()
    if not rows:
        text = "<b>📜 تاریخچه</b>\n\nهنوز اجرایی ثبت نشده است."
    else:
        lines = ["<b>📜 تاریخچه</b>", ""]
        for row in rows:
            label = row["mode"]
            params = json.loads(row["payload_json"] or "{}")
            subject = params.get("ticker") or params.get("tickers") or "—"
            lines.append(
                f"• <code>{esc(row['request_id'][:10])}</code> | {esc(subject)} | <b>{esc(row['status'])}</b> | {esc(label)}"
            )
        text = "\n".join(lines)
    return show(chat_id, text, [[("🔄 تازه‌سازی", "history"), ("🏠 خانه", "home")]], message_id)


def artifacts_screen(chat_id: int, message_id: int | None = None) -> int:
    try:
        data = gh("GET", "/actions/artifacts?per_page=20") or {}
        items = [a for a in data.get("artifacts", []) if not a.get("expired")]
    except Exception:
        items = []
    if not items:
        text = "<b>📦 خروجی‌ها</b>\n\nخروجی‌ای پیدا نشد."
    else:
        lines = ["<b>📦 خروجی‌ها</b>", ""]
        for item in items[:15]:
            lines.append(f"• <code>{esc(item.get('id'))}</code> — {esc(item.get('name'))}")
        text = "\n".join(lines)
    return show(chat_id, text, [[("🔄 تازه‌سازی", "artifacts"), ("🏠 خانه", "home")]], message_id)


def model_test_menu(chat_id: int, message_id: int | None = None) -> int:
    rows = model_rows()
    if not rows:
        return show(chat_id, "<b>🧪 تست اتصال</b>\n\nاول یک مدل اضافه کن.", [[("🤖 مدل‌ها", "models")]], message_id)
    buttons = [[(f"🧪 {row['name']}", f"test_model:{row['id']}")] for row in rows]
    buttons.append([("◀️ مدل‌ها", "models")])
    return show(
        chat_id,
        "<b>🧪 تست اتصال مدل</b>\n\nاین گزینه یک درخواست بسیار کوتاه به API مدل می‌فرستد. اگر ✅ بگیریم یعنی URL، Token و Model ID پاسخ داده‌اند. هیچ تحلیل TradingAgents اجرا نمی‌شود.",
        buttons,
        message_id,
    )


def model_delete_menu(chat_id: int, message_id: int | None = None) -> int:
    rows = model_rows()
    buttons = [[(f"🗑 {row['name']}", f"delete_model:{row['id']}")] for row in rows]
    buttons.append([("◀️ مدل‌ها", "models")])
    return show(chat_id, "<b>🗑 حذف مدل</b>\n\nمدل موردنظر را انتخاب کن.", buttons, message_id)


# ---------- Guided execution ----------
def flow_save(chat_id: int, kind: str, stage: str, **values: Any) -> None:
    current = FLOWS.get(chat_id, {})
    current.update(values)
    current.update({"kind": kind, "stage": stage})
    FLOWS[chat_id] = current


def flow_clear(chat_id: int) -> None:
    FLOWS.pop(chat_id, None)


def analysis_prompt(chat_id: int, message_id: int | None = None) -> int:
    flow_clear(chat_id)
    flow_save(chat_id, "analysis", "ticker", analysts="market,social,news,fundamentals")
    return show(
        chat_id,
        "<b>🔎 تحلیل جدید</b>\n\n<b>مرحله 1 از 2</b>\nTicker را بفرست.\nمثال: <code>NVDA</code>",
        [[("❌ لغو", "flow_cancel")]],
        message_id,
    )


def backtest_prompt(chat_id: int, message_id: int | None = None) -> int:
    flow_clear(chat_id)
    flow_save(chat_id, "backtest", "tickers", every=7, analysts="market,social,news,fundamentals")
    return show(
        chat_id,
        "<b>📈 بک‌تست</b>\n\n<b>مرحله 1 از 3</b>\nTicker یا چند Ticker را بفرست.\nمثال: <code>NVDA,AAPL</code>",
        [[("❌ لغو", "flow_cancel")]],
        message_id,
    )


def analysis_summary(chat_id: int, message_id: int) -> int:
    data = FLOWS[chat_id]
    ticker = data["ticker"]
    analysts = data.get("analysts", "")
    names = {"market": "Market", "social": "Sentiment", "news": "News", "fundamentals": "Fundamentals"}
    chosen = ", ".join(names[x] for x in analysts.split(",") if x in names) or "هیچ‌کدام"
    return show(
        chat_id,
        f"<b>✅ آماده اجرا</b>\n\nTicker: <code>{esc(ticker)}</code>\nتاریخ: <code>{esc(data['date'])}</code>\nتحلیلگران: <b>{esc(chosen)}</b>\n\nمدل فعال هم به‌صورت خودکار استفاده می‌شود.",
        [
            [("🚀 شروع تحلیل", "analysis_run"), ("🎛 تحلیلگران", "analysis_analysts")],
            [("📅 تغییر تاریخ", "analysis_change_date")],
            [("❌ لغو", "flow_cancel")],
        ],
        message_id,
    )


def backtest_summary(chat_id: int, message_id: int) -> int:
    data = FLOWS[chat_id]
    names = {"market": "Market", "social": "Sentiment", "news": "News", "fundamentals": "Fundamentals"}
    chosen = ", ".join(names[x] for x in data.get("analysts", "").split(",") if x in names) or "هیچ‌کدام"
    return show(
        chat_id,
        f"<b>✅ آماده اجرا</b>\n\nTickerها: <code>{esc(data['tickers'])}</code>\nشروع: <code>{esc(data['start'])}</code>\nپایان: <code>{esc(data['end'])}</code>\nفاصله: <b>هر {int(data.get('every', 7))} روز</b>\nتحلیلگران: <b>{esc(chosen)}</b>",
        [
            [("🚀 شروع بک‌تست", "backtest_run"), ("📅 فاصله", "backtest_every")],
            [("🎛 تحلیلگران", "backtest_analysts")],
            [("❌ لغو", "flow_cancel")],
        ],
        message_id,
    )


def analyst_picker(chat_id: int, message_id: int, backtest: bool = False) -> int:
    data = FLOWS[chat_id]
    selected = set(filter(None, data.get("analysts", "").split(",")))
    allowed = ["market", "social", "news", "fundamentals"]
    ticker = data.get("ticker", "")
    if ticker and is_crypto(ticker):
        allowed.remove("fundamentals")
        selected.discard("fundamentals")
        data["analysts"] = ",".join(x for x in allowed if x in selected)

    labels = {"market": "📊 Market", "social": "💬 Sentiment", "news": "📰 News", "fundamentals": "💰 Fundamentals"}
    rows = []
    for i in range(0, len(allowed), 2):
        pair = allowed[i:i + 2]
        rows.append([(("✅ " if x in selected else "") + labels[x], f"toggle_analyst:{x}") for x in pair])
    rows += [[("✅ استفاده از انتخاب", "analysts_done")], [("❌ لغو", "flow_cancel")]]
    flow_save(chat_id, "backtest" if backtest else "analysis", "analysts")
    return show(chat_id, "<b>🎛 تحلیلگران</b>\n\nموردهای انتخاب‌شده با ✅ مشخص‌اند.", rows, message_id)


def every_picker(chat_id: int, message_id: int) -> int:
    rows = [
        [("📅 هر 1 روز", "every:1"), ("📅 هر 7 روز", "every:7")],
        [("📅 هر 14 روز", "every:14"), ("📅 هر 30 روز", "every:30")],
        [("◀️ بازگشت", "backtest_summary")],
    ]
    return show(chat_id, "<b>📅 فاصله بک‌تست</b>\n\nپیش‌فرض <b>7 روز</b> است.", rows, message_id)


def finish_flow(chat_id: int, message_id: int) -> None:
    data = FLOWS.get(chat_id)
    if not data:
        raise ValueError("این مرحله دیگر فعال نیست؛ دوباره از اجرای جدید شروع کن.")

    mode = data["kind"]
    if mode == "analysis":
        params = {
            "ticker": data["ticker"],
            "date": data["date"],
            "analysts": data.get("analysts", ""),
        }
    else:
        params = {
            "tickers": data["tickers"],
            "start": data["start"],
            "end": data["end"],
            "every": int(data.get("every", 7)),
            "analysts": data.get("analysts", ""),
        }

    request_id, model_id = dispatch_run(chat_id, mode, params)
    model = DB.execute("SELECT name FROM models WHERE id=?", (model_id,)).fetchone()
    flow_clear(chat_id)
    label = "تحلیل" if mode == "analysis" else "بک‌تست"
    show(
        chat_id,
        f"<b>🚀 {label} در صف اجرا قرار گرفت</b>\n\nشناسه: <code>{esc(request_id)}</code>\nمدل: <b>{esc(model['name'] if model else model_id)}</b>\n\nبرای مشاهده فقط «اجراهای جاری» یا «تاریخچه» را باز کن.",
        [[("📊 اجراهای جاری", "active_runs"), ("📜 تاریخچه", "history")], [("🏠 خانه", "home")]],
        message_id,
    )


def handle_flow_text(chat_id: int, text: str, message_id: int) -> bool:
    data = FLOWS.get(chat_id)
    if not data:
        return False
    try:
        if data["kind"] == "analysis":
            if data["stage"] == "ticker":
                data["ticker"] = valid_ticker(text)
                flow_save(chat_id, "analysis", "date")
                show(chat_id, "<b>مرحله 2 از 2</b>\nتاریخ تحلیل را بفرست یا دکمه «امروز» را بزن.", [[("📅 امروز", "analysis_today"), ("❌ لغو", "flow_cancel")]], message_id)
                return True
            if data["stage"] == "date":
                data["date"] = valid_date(text)
                analysis_summary(chat_id, message_id)
                return True
            if data["stage"] == "analysts":
                return True

        if data["kind"] == "backtest":
            if data["stage"] == "tickers":
                data["tickers"] = valid_ticker(text, allow_many=True)
                flow_save(chat_id, "backtest", "start")
                show(chat_id, "<b>مرحله 2 از 3</b>\nتاریخ شروع را بفرست.\nمثال: <code>2026-01-01</code>", [[("❌ لغو", "flow_cancel")]], message_id)
                return True
            if data["stage"] == "start":
                data["start"] = valid_date(text)
                flow_save(chat_id, "backtest", "end")
                show(chat_id, "<b>مرحله 3 از 3</b>\nتاریخ پایان را بفرست.", [[("📅 امروز", "backtest_today"), ("❌ لغو", "flow_cancel")]], message_id)
                return True
            if data["stage"] == "end":
                data["end"] = valid_date(text)
                if data["end"] < data["start"]:
                    raise ValueError("تاریخ پایان باید بعد از شروع باشد.")
                backtest_summary(chat_id, message_id)
                return True
    except ValueError as exc:
        show(chat_id, f"<b>⚠️ ورودی درست نیست</b>\n\n{esc(exc)}\n\nهمین مرحله را دوباره وارد کن.", [[("❌ لغو", "flow_cancel")]], message_id)
        return True
    return True


# ---------- Model wizard ----------
def wizard_start(chat_id: int, message_id: int) -> int:
    WIZARDS[chat_id] = {"stage": "url"}
    return show(
        chat_id,
        "<b>➕ افزودن مدل</b>\n\n<b>1 از 3 — Base URL</b>\nلینک پایه API را بفرست.\nمثال: <code>https://integrate.api.nvidia.com/v1</code>\n\nلازم نیست Provider را انتخاب کنی؛ از روی URL تشخیص داده می‌شود.",
        [[("❌ لغو", "wizard_cancel")]],
        message_id,
    )


def wizard_prompt_token(chat_id: int, message_id: int) -> int:
    return show(chat_id, "<b>2 از 3 — API Token</b>\nتوکن را بفرست.", [[("❌ لغو", "wizard_cancel")]], message_id)


def wizard_prompt_model(chat_id: int, message_id: int) -> int:
    return show(chat_id, "<b>3 از 3 — Model ID</b>\nنام دقیق مدل را دقیقاً همان‌طور که Provider اعلام کرده بفرست.", [[("❌ لغو", "wizard_cancel")]], message_id)


def wizard_confirm(chat_id: int, message_id: int) -> int:
    item = WIZARDS[chat_id]
    return show(
        chat_id,
        f"<b>🔎 بررسی نهایی</b>\n\nProvider: <code>{esc(item['provider'])}</code>\nModel: <code>{esc(item['model'])}</code>\nBase URL: <code>{esc(item['url'])}</code>\n\nهمه‌چیز را بررسی کردی؟",
        [[("✅ ذخیره و فعال‌سازی", "wizard_save")], [("✏️ اصلاح URL", "wizard_edit_url"), ("✏️ اصلاح Model", "wizard_edit_model")], [("❌ لغو", "wizard_cancel")]],
        message_id,
    )


def handle_wizard_text(chat_id: int, text: str, message_id: int) -> bool:
    item = WIZARDS.get(chat_id)
    if not item:
        return False
    try:
        stage = item["stage"]
        if stage == "url":
            item["url"] = normalize_base_url(text)
            item["provider"] = infer_provider(item["url"])
            item["stage"] = "token"
            wizard_prompt_token(chat_id, message_id)
            return True
        if stage == "token":
            if len(text.strip()) < 3:
                raise ValueError("Token خیلی کوتاه است.")
            item["token"] = text.strip()
            item["stage"] = "model"
            wizard_prompt_model(chat_id, message_id)
            return True
        if stage == "model":
            if not text.strip():
                raise ValueError("Model ID نمی‌تواند خالی باشد.")
            item["model"] = text.strip()
            model_id = add_model(item["model"], item["provider"], item["url"], item["token"])
            WIZARDS.pop(chat_id, None)
            show(
                chat_id,
                f"<b>✅ مدل ذخیره و فعال شد</b>\n\nModel: <code>{esc(item['model'])}</code>\nProvider: <code>{esc(item['provider'])}</code>\n\nاکنون آماده اجراست.",
                [[("🧪 تست اتصال", f"test_model:{model_id}")], [("🚀 اجرای جدید", "run"), ("🏠 خانه", "home")]],
                message_id,
            )
            return True
    except ValueError as exc:
        show(chat_id, f"<b>⚠️ {esc(exc)}</b>", [[("❌ لغو", "wizard_cancel")]], message_id)
        return True
    return True


# ---------- Update handling ----------
def handle_message(message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = int(message["from"]["id"])
    text = (message.get("text") or "").strip()
    if not authorized(user_id):
        return

    mid = ui_message(chat_id)
    if text.startswith("/start"):
        WIZARDS.pop(chat_id, None)
        flow_clear(chat_id)
        home(chat_id, mid)
        return
    if handle_wizard_text(chat_id, text, mid or 0):
        return
    if handle_flow_text(chat_id, text, mid or 0):
        return

    # Backward compatibility for users who already know the old commands.
    if text.lower().startswith("/analyze"):
        flow_save(chat_id, "analysis", "ticker", analysts="market,social,news,fundamentals")
        parts = text.split()
        if len(parts) >= 3:
            FLOWS[chat_id]["ticker"] = valid_ticker(parts[1])
            FLOWS[chat_id]["date"] = valid_date(parts[2])
            analysis_summary(chat_id, mid or analysis_prompt(chat_id))
        else:
            analysis_prompt(chat_id, mid or 0)
        return

    if text.lower().startswith("/backtest"):
        parts = text.split()
        if len(parts) >= 4:
            flow_save(chat_id, "backtest", "end", every=7, analysts="market,social,news,fundamentals")
            FLOWS[chat_id].update({"tickers": valid_ticker(parts[1], True), "start": valid_date(parts[2]), "end": valid_date(parts[3])})
            if FLOWS[chat_id]["end"] < FLOWS[chat_id]["start"]:
                raise ValueError("تاریخ پایان باید بعد از شروع باشد.")
            backtest_summary(chat_id, mid or backtest_prompt(chat_id))
        else:
            backtest_prompt(chat_id, mid or 0)


# ---------- Callback handling ----------
def callback(query: dict[str, Any]) -> None:
    user_id = int(query.get("from", {}).get("id", 0))
    if not authorized(user_id):
        answer_callback(query["id"], "دسترسی مجاز نیست.", True)
        return
    answer_callback(query["id"])

    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    message_id = int(message.get("message_id", ui_message(chat_id) or 0))
    data = query.get("data", "")

    try:
        if data == "home":
            WIZARDS.pop(chat_id, None)
            flow_clear(chat_id)
            home(chat_id, message_id)
            return
        if data == "run":
            if not active_model_id():
                model_screen(chat_id, message_id)
            else:
                run_menu(chat_id, message_id)
            return
        if data == "models":
            model_screen(chat_id, message_id)
            return
        if data == "active_runs":
            active_runs_screen(chat_id, message_id)
            return
        if data == "history":
            history_screen(chat_id, message_id)
            return
        if data == "artifacts":
            artifacts_screen(chat_id, message_id)
            return

        if data == "model_add":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل اضافه کند.")
            wizard_start(chat_id, message_id)
            return
        if data == "wizard_cancel":
            WIZARDS.pop(chat_id, None)
            model_screen(chat_id, message_id)
            return
        if data == "wizard_edit_url":
            item = WIZARDS.get(chat_id)
            if item:
                item["stage"] = "url"
                show(chat_id, "<b>اصلاح Base URL</b>\n\nURL جدید را بفرست.", [[("❌ لغو", "wizard_cancel")]], message_id)
            return
        if data == "wizard_edit_model":
            item = WIZARDS.get(chat_id)
            if item:
                item["stage"] = "model"
                show(chat_id, "<b>اصلاح Model ID</b>\n\nنام دقیق مدل را بفرست.", [[("❌ لغو", "wizard_cancel")]], message_id)
            return
        if data == "wizard_save":
            raise ValueError("این مرحله دیگر استفاده نمی‌شود؛ بعد از وارد کردن Model ID ذخیره به‌صورت خودکار انجام می‌شود.")
        if data == "model_test_menu":
            model_test_menu(chat_id, message_id)
            return
        if data.startswith("test_model:"):
            model_id = data.split(":", 1)[1]
            row = DB.execute("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
            if not row:
                raise ValueError("مدل پیدا نشد.")
            try:
                elapsed = post_model_test(row)
            except Exception as exc:
                code = http_error_code(exc)
                detail = f"HTTP {code}" if code else "پاسخ معتبر از API دریافت نشد"
                show(chat_id, f"<b>❌ تست ناموفق</b>\n\nمدل: <code>{esc(row['name'])}</code>\n{esc(detail)}\n\nTradingAgents اجرا نشد.", [[("🧪 تست دوباره", f"test_model:{model_id}"), ("◀️ مدل‌ها", "models")]], message_id)
                return
            show(chat_id, f"<b>✅ تست موفق</b>\n\nمدل: <code>{esc(row['name'])}</code>\nProvider: <code>{esc(row['provider'])}</code>\nزمان پاسخ: <b>{elapsed:.1f}s</b>\n\nAPI پاسخ داده؛ هیچ تحلیل TradingAgents اجرا نشد.", [[("🧪 تست دوباره", f"test_model:{model_id}"), ("◀️ مدل‌ها", "models")]], message_id)
            return
        if data.startswith("activate:"):
            model_id = data.split(":", 1)[1]
            row = DB.execute("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
            if not row:
                raise ValueError("مدل پیدا نشد.")
            write_active_model_secrets(row)
            set_active_model(model_id)
            model_screen(chat_id, message_id)
            return
        if data == "model_delete_menu":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            model_delete_menu(chat_id, message_id)
            return
        if data.startswith("delete_model:"):
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            model_id = data.split(":", 1)[1]
            db("UPDATE models SET enabled=0,updated_at=? WHERE id=?", (now(), model_id))
            if active_model_id() == model_id:
                set_setting("active_model", "")
            model_screen(chat_id, message_id)
            return

        if data == "flow_analysis_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            analysis_prompt(chat_id, message_id)
            return
        if data == "flow_backtest_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            backtest_prompt(chat_id, message_id)
            return
        if data == "flow_cancel":
            flow_clear(chat_id)
            WIZARDS.pop(chat_id, None)
            home(chat_id, message_id)
            return
        if data == "analysis_today":
            data2 = FLOWS.get(chat_id)
            if not data2 or data2.get("kind") != "analysis":
                raise ValueError("این مرحله منقضی شده است.")
            data2["date"] = dt.date.today().isoformat()
            analysis_summary(chat_id, message_id)
            return
        if data == "backtest_today":
            data2 = FLOWS.get(chat_id)
            if not data2 or data2.get("kind") != "backtest":
                raise ValueError("این مرحله منقضی شده است.")
            data2["end"] = dt.date.today().isoformat()
            if data2["end"] < data2["start"]:
                raise ValueError("امروز قبل از تاریخ شروع است.")
            backtest_summary(chat_id, message_id)
            return
        if data == "analysis_change_date":
            data2 = FLOWS.get(chat_id)
            if not data2:
                raise ValueError("این مرحله منقضی شده است.")
            data2["stage"] = "date"
            show(chat_id, "تاریخ جدید را بفرست یا «امروز» را بزن.", [[("📅 امروز", "analysis_today"), ("❌ لغو", "flow_cancel")]], message_id)
            return
        if data == "analysis_analysts":
            analyst_picker(chat_id, message_id, backtest=False)
            return
        if data == "backtest_analysts":
            analyst_picker(chat_id, message_id, backtest=True)
            return
        if data.startswith("toggle_analyst:"):
            data2 = FLOWS.get(chat_id)
            if not data2:
                raise ValueError("این مرحله منقضی شده است.")
            key = data.split(":", 1)[1]
            selected = set(filter(None, data2.get("analysts", "").split(",")))
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
            order = ["market", "social", "news", "fundamentals"]
            data2["analysts"] = ",".join(x for x in order if x in selected)
            analyst_picker(chat_id, message_id, data2.get("kind") == "backtest")
            return
        if data == "analysts_done":
            data2 = FLOWS.get(chat_id)
            if not data2:
                raise ValueError("این مرحله منقضی شده است.")
            if not data2.get("analysts"):
                raise ValueError("حداقل یک تحلیلگر انتخاب کن.")
            if data2["kind"] == "analysis":
                data2["stage"] = "date_done"
                analysis_summary(chat_id, message_id)
            else:
                data2["stage"] = "summary"
                backtest_summary(chat_id, message_id)
            return
        if data == "backtest_every":
            every_picker(chat_id, message_id)
            return
        if data.startswith("every:"):
            data2 = FLOWS.get(chat_id)
            if not data2:
                raise ValueError("این مرحله منقضی شده است.")
            data2["every"] = int(data.split(":", 1)[1])
            backtest_summary(chat_id, message_id)
            return
        if data == "backtest_summary":
            backtest_summary(chat_id, message_id)
            return
        if data == "analysis_run":
            finish_flow(chat_id, message_id)
            return
        if data == "backtest_run":
            finish_flow(chat_id, message_id)
            return

    except Exception as exc:
        show(chat_id, f"<b>❌ خطا</b>\n\n{esc(exc)}", [[("🏠 خانه", "home")]], message_id)


# ---------- Polling ----------
def get_last_update_id() -> int:
    value = get_setting("last_update_id", "")
    try:
        return int(value)
    except ValueError:
        return 0


def set_last_update_id(value: int) -> None:
    set_setting("last_update_id", str(value))


def poll() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")

    me = tg("getMe")
    print(f"TradingAgents controller started @{me.get('username', '')}", flush=True)
    offset = get_last_update_id() + 1
    conflict_started = 0.0

    while True:
        try:
            updates = tg(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": ["message", "callback_query"],
                },
            ) or []
            conflict_started = 0.0
            for update in updates:
                update_id = int(update["update_id"])
                offset = max(offset, update_id + 1)
                # Commit the cursor BEFORE handling the update. A restart can
                # therefore never replay an already-accepted button/text event.
                set_last_update_id(update_id)
                try:
                    if "callback_query" in update:
                        callback(update["callback_query"])
                    elif "message" in update:
                        handle_message(update["message"])
                except Exception:
                    traceback.print_exc()
        except Exception as exc:
            message = str(exc)
            # Telegram returns 409 when another controller owns getUpdates. Do
            # not spin and spam the log; give the old instance time to exit.
            if "409" in message or "Conflict" in message:
                if not conflict_started:
                    conflict_started = time.monotonic()
                    print("Another Telegram controller instance is active; waiting...", flush=True)
                if time.monotonic() - conflict_started > 120:
                    print("A second Telegram controller is still active; stopping this controller.", flush=True)
                    return
                time.sleep(10)
            else:
                print(f"Polling error: {message}", flush=True)
                time.sleep(5)


if __name__ == "__main__":
    poll()
