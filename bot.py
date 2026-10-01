#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-file Telegram controller for TradingAgentsArya.

- GitHub Actions is the execution plane.
- Telegram is the control plane.
- Model setup is a guided Telegram wizard; no JSON is required.
- Repository-dispatch payload is kept within GitHub's 10 top-level-property limit.
- Menus are edited in-place instead of creating a new menu message on every click.
"""
from __future__ import annotations

import base64
import hashlib
import datetime as dt
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
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
REF = os.getenv("GITHUB_REF", "main").strip()
STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"

ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_IDS = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH = f"https://api.github.com/repos/{OWNER}/{REPO}"

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS models(
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        base_url TEXT DEFAULT '',
        region TEXT DEFAULT '',
        token_ciphertext BLOB DEFAULT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs(
        request_id TEXT PRIMARY KEY,
        workflow_run_id INTEGER,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        analysts TEXT DEFAULT '',
        model_id TEXT DEFAULT '',
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS wizard(
        chat_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        stage TEXT NOT NULL,
        base_url TEXT DEFAULT '',
        token TEXT DEFAULT '',
        model_name TEXT DEFAULT '',
        provider TEXT DEFAULT 'openai_compatible',
        prompt_message_id INTEGER,
        updated_at TEXT NOT NULL
    );
    """
)
DB.commit()

# Backward-compatible schema upgrade for an older bot_state.db.
try:
    DB.execute("ALTER TABLE models ADD COLUMN token_ciphertext BLOB DEFAULT NULL")
    DB.commit()
except sqlite3.OperationalError:
    pass


def token_box():
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN is required")
    try:
        from nacl.secret import SecretBox
    except ImportError as exc:
        raise RuntimeError("PyNaCl نصب نشده است.") from exc
    key = hashlib.sha256((OWNER + ":" + REPO + ":" + GH_TOKEN).encode()).digest()
    return SecretBox(key)


def encrypt_token(value: str) -> bytes:
    return bytes(token_box().encrypt(value.encode()))


def decrypt_token(value: bytes | bytearray | memoryview | None) -> str:
    if not value:
        return ""
    return token_box().decrypt(bytes(value)).decode()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def http_json(url: str, method: str = "GET", data: Any = None, headers: dict[str, str] | None = None, timeout: int = 60):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    request_headers = {"Accept": "application/vnd.github+json"}
    if headers:
        request_headers.update(headers)
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            obj = raw
        raise RuntimeError(f"HTTP {err.code}: {obj}") from err


def tg(method: str, payload: dict[str, Any] | None = None):
    _, obj = http_json(f"{TG}/{method}", "POST", payload or {}, {"Content-Type": "application/json"})
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API error: {obj}")
    return obj.get("result")


def gh(method: str, path: str, data: Any = None):
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN is required")
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TradingAgentsArya-Telegram",
    }
    status, obj = http_json(GH + path, method, data, headers)
    if status >= 300:
        raise RuntimeError(f"GitHub API error {status}: {obj}")
    return obj


def db(sql: str, args: tuple = ()):
    cur = DB.execute(sql, args)
    DB.commit()
    return cur


def send(chat_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)


def edit(chat_id: int, message_id: int, text: str, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return tg("editMessageText", payload)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return None
        raise


def answer_cb(query_id: str, text: str = "", alert: bool = False):
    tg("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})


def kb(rows):
    return [[{"text": label, "callback_data": data} for label, data in row] for row in rows]


def authorized(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in ALLOWED_IDS


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def model_rows():
    return DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()


def active_model_id() -> str:
    row = DB.execute("SELECT value FROM settings WHERE key='active_model'").fetchone()
    return str(row[0]) if row else ""


def set_active(model_id: str):
    db(
        "INSERT INTO settings(key,value) VALUES('active_model',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (model_id,),
    )


def github_secret(name: str, value: str):
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise RuntimeError("PyNaCl نصب نشده است.") from exc
    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode())
    gh(
        "PUT",
        f"/actions/secrets/{urllib.parse.quote(name, safe='')}",
        {"encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key["key_id"]},
    )


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
}
NO_KEY_PROVIDERS = {"bedrock", "ollama"}


def infer_provider(base_url: str) -> str:
    host = urllib.parse.urlparse(base_url).netloc.lower()
    candidates = [
        ("api.openai.com", "openai"),
        ("api.anthropic.com", "anthropic"),
        ("generativelanguage.googleapis.com", "google"),
        ("api.x.ai", "xai"),
        ("api.deepseek.com", "deepseek"),
        ("openrouter.ai", "openrouter"),
        ("api.mistral.ai", "mistral"),
        ("api.groq.com", "groq"),
        ("api.moonshot.ai", "kimi"),
        ("api.minimax.io", "minimax"),
    ]
    for needle, provider in candidates:
        if needle in host:
            return provider
    return "openai_compatible"


def activate_model(model_id: str, token: str | None = None):
    row = DB.execute("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
    if not row:
        raise ValueError("مدل پیدا نشد.")
    provider = row["provider"].lower().strip()
    if provider not in PROVIDER_SECRET and provider not in NO_KEY_PROVIDERS:
        raise ValueError(f"Provider پشتیبانی‌شده نیست: {provider}")

    if provider in PROVIDER_SECRET:
        if token:
            github_secret(PROVIDER_SECRET[provider], token)
        else:
            token = decrypt_token(row["token_ciphertext"])
            if not token:
                raise ValueError("توکن این مدل پیدا نشد؛ مدل را دوباره تنظیم کن.")
            github_secret(PROVIDER_SECRET[provider], token)
    if provider == "ollama":
        github_secret("OLLAMA_BASE_URL", row["base_url"] or "http://localhost:11434/v1")
    elif provider == "bedrock":
        if not row["region"]:
            raise ValueError("برای Bedrock باید region ثبت شود.")
        github_secret("AWS_DEFAULT_REGION", row["region"])

    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", row["model"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", row["model"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", row["base_url"] if provider != "ollama" else "")
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")
    set_active(model_id)
    return row


def dispatch_run(mode: str, *, ticker: str = "", date: str = "", tickers: str = "", start: str = "", end: str = "", every: int = 7, analysts: str = "", portfolio_json: str = ""):
    model_id = active_model_id()
    if not model_id:
        raise ValueError("هنوز هیچ مدل فعالی ثبت نشده است. اول از بخش مدل‌ها مدل اضافه و فعال کن.")

    request_id = uuid.uuid4().hex
    # Exactly 10 top-level client_payload properties: GitHub rejects 11+.
    payload = {
        "mode": mode,
        "ticker": ticker.upper().strip(),
        "date": date.strip(),
        "tickers": tickers.strip(),
        "start": start.strip(),
        "end": end.strip(),
        "every": str(every),
        "analysts": analysts.strip(),
        "portfolio_json": portfolio_json,
        "request_id": request_id,
    }
    gh("POST", "/dispatches", {"event_type": "tradingagents_run", "client_payload": payload})

    display_ticker = payload["ticker"] or payload["tickers"]
    display_date = payload["date"] or f"{payload['start']} → {payload['end']}"
    db(
        "INSERT INTO runs(request_id,ticker,date,analysts,model_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (request_id, display_ticker, display_date, payload["analysts"], model_id, "queued", now(), now()),
    )
    return request_id, model_id


def workflow_runs():
    data = gh("GET", "/actions/runs?per_page=15")
    return data.get("workflow_runs", []) if isinstance(data, dict) else []


def menu(chat_id: int, text: str, rows, message_id: int | None = None):
    keyboard = kb(rows)
    if message_id:
        try:
            edit(chat_id, message_id, text, keyboard)
            return message_id
        except Exception:
            pass
    result = send(chat_id, text, keyboard)
    return int(result["message_id"])


def home_text() -> str:
    active = active_model_id()
    model = DB.execute("SELECT name,model FROM models WHERE id=?", (active,)).fetchone() if active else None
    active_text = f"{esc(model['name'])} — <code>{esc(model['model'])}</code>" if model else "تنظیم نشده"
    return f"<b>TradingAgents</b>\nمدل فعال: {active_text}\n\nکنترلر آماده است."


def main_menu(chat_id: int, message_id: int | None = None):
    return menu(chat_id, home_text(), [
        [("▶️ اجرا", "run"), ("📊 وضعیت", "status")],
        [("🤖 مدل‌ها", "models"), ("🧪 تست مدل", "tests")],
        [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")],
    ], message_id)


def model_menu(chat_id: int, message_id: int | None = None):
    rows = model_rows()
    active = active_model_id()
    lines = ["<b>🤖 مدل‌ها</b>"]
    if not rows:
        lines.append("هنوز مدلی ثبت نشده است.")
    for row in rows:
        mark = " ✅" if row["id"] == active else ""
        lines.append(f"• {esc(row['name'])} — <code>{esc(row['model'])}</code>{mark}")
    buttons = [[("➕ افزودن مدل", "add")]]
    for row in rows:
        buttons.append([(f"▶️ فعال: {row['name']}", f"activate:{row['id']}")])
    buttons += [[("🗑 حذف مدل", "delmenu")], [("◀️ خانه", "home")]]
    return menu(chat_id, "\n".join(lines), buttons, message_id)


def run_menu(chat_id: int, message_id: int | None = None):
    if not model_rows():
        return menu(chat_id, "⚠️ ابتدا یک مدل اضافه و فعال کن.", [[("🤖 مدل‌ها", "models")]], message_id)
    return menu(chat_id,
        "<b>▶️ اجرا</b>\n\n"
        "تحلیل: <code>/analyze NVDA 2026-09-30</code>\n"
        "بک‌تست: <code>/backtest NVDA,AAPL 2026-01-01 2026-09-30 7</code>\n\n"
        "تحلیل‌گرها را می‌توانی در انتهای دستور مشخص کنی: <code>market,news,fundamentals,social</code>",
        [[("📈 تحلیل نمونه", "sample")], [("◀️ خانه", "home")]], message_id)


def status_menu(chat_id: int, message_id: int | None = None):
    runs = workflow_runs()
    lines = ["<b>📊 وضعیت اجراها</b>"]
    for run in runs[:8]:
        lines.append(f"• <code>{run.get('id')}</code> — {esc(run.get('status'))}/{esc(run.get('conclusion') or 'running')}")
    if len(lines) == 1:
        lines.append("اجرایی پیدا نشد.")
    return menu(chat_id, "\n".join(lines), [[("🔄 تازه‌سازی", "status"), ("◀️ خانه", "home")]], message_id)


def history_menu(chat_id: int, message_id: int | None = None):
    rows = DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 12").fetchall()
    text = "<b>📜 تاریخچه</b>\n"
    text += "اجرایی ثبت نشده است." if not rows else "\n".join(
        f"• <code>{esc(row['request_id'][:10])}</code> — {esc(row['ticker'])} — {esc(row['status'])}" for row in rows
    )
    return menu(chat_id, text, [[("◀️ خانه", "home")]], message_id)


def artifacts_menu(chat_id: int, message_id: int | None = None):
    data = gh("GET", "/actions/artifacts?per_page=20")
    items = data.get("artifacts", []) if isinstance(data, dict) else []
    text = "<b>📦 خروجی‌ها</b>\n" + ("موردی نیست." if not items else "\n".join(f"• <code>{a.get('id')}</code> — {esc(a.get('name'))}" for a in items[:12]))
    return menu(chat_id, text, [[("🔄 تازه‌سازی", "artifacts"), ("◀️ خانه", "home")]], message_id)


def test_menu(chat_id: int, message_id: int | None = None):
    rows = model_rows()
    if not rows:
        return menu(chat_id, "⚠️ هنوز مدلی ثبت نشده است.", [[("🤖 مدل‌ها", "models")]], message_id)
    buttons = [[(f"🧪 {row['name']}", f"test:{row['id']}")] for row in rows]
    buttons.append([("◀️ خانه", "home")])
    return menu(chat_id, "<b>🧪 تست مدل</b>\nمدل را انتخاب کن:", buttons, message_id)


def delete_menu(chat_id: int, message_id: int | None = None):
    rows = model_rows()
    buttons = [[(f"🗑 {row['name']}", f"delete:{row['id']}")] for row in rows]
    buttons.append([("◀️ مدل‌ها", "models")])
    return menu(chat_id, "<b>حذف مدل</b>\nمدل را انتخاب کن:", buttons, message_id)


def wizard_row(chat_id: int):
    return DB.execute("SELECT * FROM wizard WHERE chat_id=?", (chat_id,)).fetchone()


def wizard_save(chat_id: int, user_id: int, stage: str, *, base_url: str = "", token: str = "", model_name: str = "", provider: str = "openai_compatible", prompt_message_id: int | None = None):
    db(
        "INSERT INTO wizard(chat_id,user_id,stage,base_url,token,model_name,provider,prompt_message_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,stage=excluded.stage,base_url=excluded.base_url,token=excluded.token,model_name=excluded.model_name,provider=excluded.provider,prompt_message_id=excluded.prompt_message_id,updated_at=excluded.updated_at",
        (chat_id, user_id, stage, base_url, token, model_name, provider, prompt_message_id, now()),
    )


def wizard_clear(chat_id: int):
    db("DELETE FROM wizard WHERE chat_id=?", (chat_id,))


def wizard_prompt(chat_id: int, user_id: int, stage: str, text: str, *, base_url: str = "", token: str = "", model_name: str = "", provider: str = "openai_compatible", message_id: int | None = None):
    buttons = [[("❌ لغو", "wizard_cancel")]]
    if message_id:
        edit(chat_id, message_id, text, kb(buttons))
        wizard_save(chat_id, user_id, stage, base_url=base_url, token=token, model_name=model_name, provider=provider, prompt_message_id=message_id)
        return message_id
    result = send(chat_id, text, kb(buttons))
    new_id = int(result["message_id"])
    wizard_save(chat_id, user_id, stage, base_url=base_url, token=token, model_name=model_name, provider=provider, prompt_message_id=new_id)
    return new_id


def start_model_wizard(chat_id: int, user_id: int, message_id: int | None = None):
    return wizard_prompt(chat_id, user_id, "base_url", "<b>➕ افزودن مدل</b>\n\n1/3 — <b>Base URL</b> را بفرست.\nمثال: <code>https://integrate.api.nvidia.com/v1</code>\n\nاگر Provider داخلی است و Base URL لازم ندارد، <code>-</code> بفرست.", message_id=message_id)


def finish_model(chat_id: int, user_id: int, w: sqlite3.Row, provider: str):
    model_name = w["model_name"].strip()
    base_url = "" if w["base_url"] == "-" else w["base_url"].strip()
    token = w["token"].strip()
    if not model_name or not token:
        raise ValueError("Model name و Token الزامی هستند.")
    if provider not in PROVIDER_SECRET and provider not in NO_KEY_PROVIDERS:
        provider = "openai_compatible"
    model_id = "m_" + uuid.uuid4().hex[:10]
    timestamp = now()
    db(
        "INSERT INTO models(id,name,provider,model,base_url,region,token_ciphertext,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
        (model_id, model_name, provider, model_name, base_url, "", encrypt_token(token), timestamp, timestamp),
    )
    github_secret(PROVIDER_SECRET[provider], token)
    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", model_name)
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", model_name)
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", base_url)
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")
    set_active(model_id)
    wizard_clear(chat_id)
    mid = w["prompt_message_id"]
    text = f"✅ <b>مدل آماده شد</b>\nمدل: <code>{esc(model_name)}</code>\nProvider: <code>{esc(provider)}</code>"
    if mid:
        try:
            edit(chat_id, mid, text, kb([[('▶️ اجرا', 'run'), ('🧪 تست', 'tests')], [('🤖 مدل‌ها', 'models')]]))
            return
        except Exception:
            pass
    send(chat_id, text, kb([[('▶️ اجرا', 'run'), ('🧪 تست', 'tests')], [('🤖 مدل‌ها', 'models')]]))


def handle_wizard_text(message: dict) -> bool:
    chat_id = int(message["chat"]["id"])
    user_id = int(message["from"]["id"])
    w = wizard_row(chat_id)
    if not w:
        return False
    if w["user_id"] != user_id or not is_admin(user_id):
        return False
    text = (message.get("text") or "").strip()
    if not text:
        return True
    mid = w["prompt_message_id"]
    try:
        if w["stage"] == "base_url":
            base_url = "" if text == "-" else text
            if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
                raise ValueError("Base URL باید با http:// یا https:// شروع شود.")
            provider = infer_provider(base_url) if base_url else "openai_compatible"
            wizard_prompt(chat_id, user_id, "token", "<b>2/3 — Token</b>\nتوکن API را بفرست.\nبعد از ثبت، خود توکن در دیتابیس ربات ذخیره نمی‌شود.", base_url=base_url, provider=provider, message_id=mid)
            return True
        if w["stage"] == "token":
            if len(text) < 3:
                raise ValueError("Token معتبر نیست.")
            wizard_prompt(chat_id, user_id, "model_name", "<b>3/3 — Model name</b>\nنام دقیق مدل را بفرست.\nمثال: <code>openai/gpt-oss-20b</code>", base_url=w["base_url"], token=text, provider=w["provider"], message_id=mid)
            return True
        if w["stage"] == "model_name":
            wizard_save(chat_id, user_id, "confirm", base_url=w["base_url"], token=w["token"], model_name=text, provider=w["provider"], prompt_message_id=mid)
            buttons = [
                [("⚡ OpenAI-compatible", "provider:openai_compatible")],
                [("OpenAI", "provider:openai"), ("Anthropic", "provider:anthropic")],
                [("Google", "provider:google"), ("DeepSeek", "provider:deepseek")],
                [("NVIDIA", "provider:nvidia"), ("OpenRouter", "provider:openrouter")],
                [("◀️ لغو", "wizard_cancel")],
            ]
            edit(chat_id, mid, f"<b>Provider</b>\nمدل: <code>{esc(text)}</code>\n\nProvider را انتخاب کن:", kb(buttons))
            return True
        return True
    except Exception as exc:
        if mid:
            edit(chat_id, mid, f"❌ {esc(exc)}\n\nدوباره همین مرحله را بفرست.", kb([[('❌ لغو', 'wizard_cancel')]]))
        return True


def handle_command(message: dict):
    chat_id = int(message["chat"]["id"])
    user_id = int(message["from"]["id"])
    text = (message.get("text") or "").strip()
    if not authorized(user_id):
        return
    if text.startswith("/start"):
        main_menu(chat_id)
        return
    if handle_wizard_text(message):
        return
    if text.startswith("/analyze"):
        parts = text.split()
        if len(parts) < 3:
            send(chat_id, "فرمت: <code>/analyze TICKER YYYY-MM-DD [analysts]</code>")
            return
        try:
            request_id, model_id = dispatch_run("analysis", ticker=parts[1], date=parts[2], analysts=parts[3] if len(parts) > 3 else "")
            send(chat_id, f"✅ تحلیل ارسال شد.\nمدل: <code>{esc(model_id)}</code>\nRequest: <code>{request_id}</code>")
        except Exception as exc:
            send(chat_id, f"❌ {esc(exc)}")
        return
    if text.startswith("/backtest"):
        parts = text.split()
        if len(parts) < 4:
            send(chat_id, "فرمت: <code>/backtest TICKERS START END [every] [analysts]</code>")
            return
        try:
            every = int(parts[4]) if len(parts) > 4 else 7
            analysts = parts[5] if len(parts) > 5 else ""
            request_id, model_id = dispatch_run("backtest", tickers=parts[1], start=parts[2], end=parts[3], every=every, analysts=analysts)
            send(chat_id, f"✅ بک‌تست ارسال شد.\nمدل: <code>{esc(model_id)}</code>\nRequest: <code>{request_id}</code>")
        except Exception as exc:
            send(chat_id, f"❌ {esc(exc)}")
        return


def launch_test(chat_id: int, model_id: str):
    try:
        set_active(model_id)
        request_id, active = dispatch_run("analysis", ticker="AAPL", date=dt.date.today().isoformat(), analysts="market")
        send(chat_id, f"🧪 تست ارسال شد.\nمدل: <code>{esc(active)}</code>\nRequest: <code>{request_id}</code>")
    except Exception as exc:
        send(chat_id, f"❌ تست ناموفق بود:\n<code>{esc(exc)}</code>")


def callback(query: dict):
    user_id = int(query.get("from", {}).get("id", 0))
    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    message_id = int(message.get("message_id", 0))
    data = query.get("data", "")
    if not authorized(user_id):
        answer_cb(query["id"], "دسترسی مجاز نیست.", True)
        return
    answer_cb(query["id"])
    try:
        if data == "home":
            main_menu(chat_id, message_id); return
        if data == "models":
            model_menu(chat_id, message_id); return
        if data == "add":
            if not is_admin(user_id):
                answer_cb(query["id"], "فقط Admin می‌تواند مدل اضافه کند.", True); return
            start_model_wizard(chat_id, user_id, message_id); return
        if data == "wizard_cancel":
            wizard_clear(chat_id); main_menu(chat_id, message_id); return
        if data.startswith("provider:"):
            if not is_admin(user_id): return
            provider = data.split(":", 1)[1]
            w = wizard_row(chat_id)
            if not w or w["stage"] != "confirm":
                return
            finish_model(chat_id, user_id, w, provider); return
        if data.startswith("activate:"):
            if not is_admin(user_id):
                answer_cb(query["id"], "فقط Admin.", True); return
            model_id = data.split(":", 1)[1]
            activate_model(model_id)
            model_menu(chat_id, message_id)
            return
        if data == "run":
            run_menu(chat_id, message_id); return
        if data == "sample":
            try:
                rid, mid = dispatch_run("analysis", ticker="AAPL", date=dt.date.today().isoformat(), analysts="")
                edit(chat_id, message_id, f"✅ اجرای نمونه ارسال شد.\nمدل: <code>{esc(mid)}</code>\nRequest: <code>{rid}</code>", kb([[('📊 وضعیت', 'status'), ('◀️ خانه', 'home')]]))
            except Exception as exc:
                edit(chat_id, message_id, f"❌ {esc(exc)}", kb([[('◀️ خانه', 'home')]]))
            return
        if data == "status":
            status_menu(chat_id, message_id); return
        if data == "history":
            history_menu(chat_id, message_id); return
        if data == "artifacts":
            artifacts_menu(chat_id, message_id); return
        if data == "tests":
            test_menu(chat_id, message_id); return
        if data.startswith("test:"):
            if not is_admin(user_id): return
            launch_test(chat_id, data.split(":", 1)[1]); return
        if data == "delmenu":
            delete_menu(chat_id, message_id); return
        if data.startswith("delete:"):
            if not is_admin(user_id): return
            model_id = data.split(":", 1)[1]
            db("DELETE FROM models WHERE id=?", (model_id,))
            if active_model_id() == model_id:
                db("DELETE FROM settings WHERE key='active_model'")
            model_menu(chat_id, message_id); return
    except Exception as exc:
        try:
            edit(chat_id, message_id, f"❌ {esc(exc)}", kb([[('◀️ خانه', 'home')]]))
        except Exception:
            send(chat_id, f"❌ {esc(exc)}")


def poll():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")
    offset = 0
    me = tg("getMe")
    print(f"TradingAgents bot started @{me.get('username', '')}", flush=True)
    while True:
        try:
            updates = tg("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}) or []
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    if "callback_query" in update:
                        callback(update["callback_query"])
                    elif "message" in update:
                        handle_command(update["message"])
                except Exception:
                    traceback.print_exc()
        except Exception as exc:
            print(f"Polling error: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    poll()
