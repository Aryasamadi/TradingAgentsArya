#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingAgents Telegram Controller

Single-file Telegram controller for Aryasamadi/TradingAgentsArya.
No third-party runtime dependency is required for normal operation.

Required environment variables:
  TELEGRAM_BOT_TOKEN
  GITHUB_TOKEN

Optional:
  GITHUB_OWNER=Aryasamadi
  GITHUB_REPO=TradingAgentsArya
  GITHUB_REF=main
  GITHUB_WORKFLOW=.github/workflows/tradingagents.yml
  TELEGRAM_ADMIN_IDS=123,456
  TELEGRAM_ALLOWED_USER_IDS=123,456
  BOT_STATE_PATH=bot_state.db

The bot keeps its controller database locally. For a GitHub Actions deployment,
run it from a persistent runner or add an explicit backup mechanism around it.
The analysis engine itself persists TradingAgents state through the workflow artifact.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import re
import sqlite3
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GH_OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
GH_REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
GH_REF = os.getenv("GITHUB_REF", "main").strip()
GH_WORKFLOW = os.getenv("GITHUB_WORKFLOW", ".github/workflows/tradingagents.yml").strip()
DB_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"


def parse_ids(value: str) -> set[int]:
    out: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.add(int(item))
        except ValueError:
            pass
    return out


ADMIN_IDS = parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""))
ALLOWED_IDS = parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH_API = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"


# ---------------------------------------------------------------------------
# Small HTTP clients — stdlib only
# ---------------------------------------------------------------------------

class HttpError(RuntimeError):
    pass


def http_json(
    url: str,
    *,
    method: str = "GET",
    data: dict | list | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> tuple[int, Any, dict[str, str]]:
    body = None
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            text = raw.decode("utf-8", "replace")
            try:
                obj = json.loads(text) if text else None
            except json.JSONDecodeError:
                obj = text
            return r.status, obj, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            obj = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            obj = raw
        raise HttpError(f"HTTP {e.code}: {obj}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"network error: {e.reason}") from e


def tg(method: str, payload: dict[str, Any] | None = None) -> Any:
    status, obj, _ = http_json(f"{TG_API}/{method}", method="POST", data=payload or {}, timeout=60)
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise HttpError(f"Telegram API error: {obj}")
    return obj.get("result")


def gh(method: str, path: str, payload: dict | None = None, *, timeout: int = 60) -> Any:
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TradingAgents-Telegram-Controller",
    }
    status, obj, _ = http_json(GH_API + path, method=method, data=payload, headers=headers, timeout=timeout)
    if status >= 300:
        raise HttpError(f"GitHub API error {status}: {obj}")
    return obj


# ---------------------------------------------------------------------------
# SQLite state
# ---------------------------------------------------------------------------

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        base_url TEXT,
        region TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS model_secrets (
        model_id TEXT PRIMARY KEY REFERENCES models(id) ON DELETE CASCADE,
        api_key TEXT,
        aws_access_key_id TEXT,
        aws_secret_access_key TEXT,
        aws_session_token TEXT,
        ollama_base_url TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        request_id TEXT PRIMARY KEY,
        workflow_run_id INTEGER,
        mode TEXT NOT NULL,
        ticker TEXT NOT NULL,
        date_or_start TEXT,
        end_date TEXT,
        status TEXT NOT NULL,
        model_id TEXT,
        analysts TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """
)
DB.commit()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def db_execute(sql: str, args: tuple = ()) -> None:
    DB.execute(sql, args)
    DB.commit()


def db_setting(key: str, default: str = "") -> str:
    row = DB.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_setting(key: str, value: str) -> None:
    db_execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------------------
# Security / Telegram helpers
# ---------------------------------------------------------------------------

def authorized(user_id: int) -> bool:
    if not ADMIN_IDS and not ALLOWED_IDS:
        # Fail closed. The owner must explicitly configure access.
        return False
    return user_id in ADMIN_IDS or user_id in ALLOWED_IDS


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def remember_user(user: dict[str, Any]) -> None:
    uid = int(user.get("id", 0))
    db_execute(
        """INSERT INTO users(user_id,username,first_name,last_name,last_seen)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
           first_name=excluded.first_name,last_name=excluded.last_name,last_seen=excluded.last_seen""",
        (uid, user.get("username", ""), user.get("first_name", ""), user.get("last_name", ""), now()),
    )


def safe_text(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def send(chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None, *, parse_mode="HTML") -> Any:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)


def edit(chat_id: int, message_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> Any:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4096], "disable_web_page_preview": True, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("editMessageText", payload)


def answer_callback(callback_id: str, text: str = "", *, alert: bool = False) -> None:
    tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert})


def buttons(rows: list[tuple[str, str]] | list[list[tuple[str, str]]]) -> list[list[dict[str, str]]]:
    if rows and isinstance(rows[0], tuple):
        rows = [rows]  # type: ignore[assignment]
    return [[{"text": label, "callback_data": data} for label, data in row] for row in rows]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------

def dispatch(payload: dict[str, str]) -> None:
    body = {"ref": GH_REF, "inputs": payload}
    gh("POST", f"/actions/workflows/{urllib.parse.quote(GH_WORKFLOW, safe='')}/dispatches", body)


def list_runs(limit: int = 10, *, status: str | None = None) -> list[dict[str, Any]]:
    query = f"?per_page={max(1, min(limit, 100))}"
    if status:
        query += "&status=" + urllib.parse.quote(status)
    data = gh("GET", f"/actions/runs{query}")
    return list(data.get("workflow_runs", [])) if isinstance(data, dict) else []


def get_run(run_id: int) -> dict[str, Any]:
    return gh("GET", f"/actions/runs/{int(run_id)}")


def cancel_run(run_id: int) -> None:
    gh("POST", f"/actions/runs/{int(run_id)}/cancel")


def list_artifacts(limit: int = 20) -> list[dict[str, Any]]:
    data = gh("GET", f"/actions/artifacts?per_page={max(1, min(limit, 100))}")
    return list(data.get("artifacts", [])) if isinstance(data, dict) else []


def workflow_status_text() -> str:
    runs = list_runs(10)
    if not runs:
        return "هیچ اجرای ثبت‌شده‌ای پیدا نشد."
    lines = ["<b>آخرین اجراها</b>"]
    for r in runs[:8]:
        status = r.get("status", "?")
        conclusion = r.get("conclusion") or "running"
        name = safe_text(r.get("name", "TradingAgents"))
        rid = r.get("id", "?")
        branch = safe_text(r.get("head_branch", ""))
        lines.append(f"• <code>{rid}</code> — {name} — {status}/{conclusion} — {branch}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model catalog -> GitHub secret
# ---------------------------------------------------------------------------

PROVIDER_KEYS = {
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "dashscope_cn": "DASHSCOPE_CN_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "zhipu_cn": "ZHIPU_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax_cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
    "custom": "OPENAI_COMPATIBLE_API_KEY",
    "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
}


def model_rows() -> list[sqlite3.Row]:
    return list(DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE"))


def model_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    sec = DB.execute("SELECT * FROM model_secrets WHERE model_id=?", (row["id"],)).fetchone()
    out: dict[str, Any] = {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "model": row["model"],
        "base_url": row["base_url"] or "",
        "region": row["region"] or "",
    }
    if sec:
        for key in ("api_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token", "ollama_base_url"):
            if sec[key]:
                out[key] = sec[key]
    return out


def catalog() -> dict[str, Any]:
    return {"version": 1, "models": [model_to_dict(r) for r in model_rows()]}


def put_model(row: dict[str, Any]) -> None:
    ts = now()
    db_execute(
        """INSERT INTO models(id,name,provider,model,base_url,region,enabled,created_at,updated_at)
           VALUES(?,?,?,?,?,?,1,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,
           model=excluded.model,base_url=excluded.base_url,region=excluded.region,
           enabled=1,updated_at=excluded.updated_at""",
        (row["id"], row["name"], row["provider"], row["model"], row.get("base_url", ""), row.get("region", ""), ts, ts),
    )
    db_execute(
        """INSERT INTO model_secrets(model_id,api_key,aws_access_key_id,aws_secret_access_key,aws_session_token,ollama_base_url,updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(model_id) DO UPDATE SET api_key=excluded.api_key,
           aws_access_key_id=excluded.aws_access_key_id,aws_secret_access_key=excluded.aws_secret_access_key,
           aws_session_token=excluded.aws_session_token,ollama_base_url=excluded.ollama_base_url,
           updated_at=excluded.updated_at""",
        (row["id"], row.get("api_key", ""), row.get("aws_access_key_id", ""), row.get("aws_secret_access_key", ""), row.get("aws_session_token", ""), row.get("ollama_base_url", ""), ts),
    )


# The workflow accepts the complete catalog as a single repository secret.
# PyNaCl is intentionally imported lazily: normal bot operation does not need it.
def write_repository_secret(name: str, value: str) -> None:
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise RuntimeError("برای ذخیره امن مدل‌ها، PyNaCl لازم است: python -m pip install pynacl") from exc

    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(public_key).encrypt(value.encode("utf-8"))
    body = {
        "encrypted_value": base64.b64encode(sealed).decode("ascii"),
        "key_id": key["key_id"],
    }
    gh("PUT", f"/actions/secrets/{urllib.parse.quote(name, safe='')}", body)


def sync_model_catalog() -> None:
    write_repository_secret("TRADINGAGENTS_MODEL_CONFIG_JSON", json.dumps(catalog(), ensure_ascii=False, separators=(",", ":")))


def add_or_update_model(row: dict[str, Any]) -> None:
    put_model(row)
    sync_model_catalog()


def delete_model(model_id: str) -> None:
    db_execute("DELETE FROM models WHERE id=?", (model_id,))
    sync_model_catalog()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

MAIN_KB = buttons([
    [("▶️ اجرای تحلیل", "run:menu"), ("📊 وضعیت", "status")],
    [("🤖 مدل‌ها", "models"), ("🧪 تست مدل", "testmodels")],
    [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")],
    [("⏹ توقف اجرا", "cancel:menu"), ("⚙️ تنظیمات", "settings")],
])


def main_menu(chat_id: int, title: str = "<b>TradingAgents</b>\nکنترلر GitHub آماده است.") -> None:
    send(chat_id, title, MAIN_KB)


def model_menu(chat_id: int) -> None:
    rows = model_rows()
    if not rows:
        text = "<b>مدل‌ها</b>\nهنوز مدلی ثبت نشده است."
    else:
        text = "<b>مدل‌ها</b>\n"
        for r in rows:
            text += f"• <code>{safe_text(r['id'])}</code> — {safe_text(r['name'])} — {safe_text(r['provider'])}/{safe_text(r['model'])}\n"
    kb = buttons([
        [("➕ افزودن مدل", "model:add")],
        [("✏️ ویرایش مدل", "model:edit"), ("🗑 حذف مدل", "model:delete")],
        [("◀️ بازگشت", "home")],
    ])
    send(chat_id, text, kb)


def settings_menu(chat_id: int) -> None:
    send(chat_id, "<b>تنظیمات</b>\nحالت اجرای پیش‌فرض را از دکمه زیر انتخاب کن.", buttons([
        [("🧠 تحلیل کامل", "preset:full")],
        [("⚡ تحلیل سریع", "preset:quick")],
        [("◀️ بازگشت", "home")],
    ]))


def run_menu(chat_id: int) -> None:
    send(chat_id, "<b>نوع اجرا را انتخاب کن:</b>", buttons([
        [("📈 تحلیل یک نماد", "run:analyze")],
        [("🧪 بک‌تست", "run:backtest")],
        [("◀️ بازگشت", "home")],
    ]))


def status_menu(chat_id: int) -> None:
    try:
        send(chat_id, workflow_status_text(), buttons([[("🔄 تازه‌سازی", "status"), ("◀️ بازگشت", "home")]]))
    except Exception as e:
        send(chat_id, f"❌ خطا در دریافت وضعیت:\n<code>{safe_text(e)}</code>", buttons([[("◀️ بازگشت", "home")]]))


def history_menu(chat_id: int) -> None:
    rows = DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 10").fetchall()
    if not rows:
        text = "<b>تاریخچه کنترلر</b>\nهنوز درخواستی ثبت نشده است."
    else:
        text = "<b>تاریخچه</b>\n"
        for r in rows:
            text += f"• <code>{safe_text(r['request_id'][:8])}</code> {safe_text(r['ticker'])} — {safe_text(r['status'])}\n"
    send(chat_id, text, buttons([[("◀️ بازگشت", "home")]]))


def artifacts_menu(chat_id: int) -> None:
    try:
        arts = list_artifacts(20)
        if not arts:
            text = "<b>خروجی‌ها</b>\nهیچ Artifactای پیدا نشد."
        else:
            text = "<b>آخرین Artifactها</b>\n"
            for a in arts[:10]:
                text += f"• <code>{a.get('id')}</code> — {safe_text(a.get('name'))} — {'منقضی' if a.get('expired') else 'فعال'}\n"
        send(chat_id, text, buttons([[("🔄 تازه‌سازی", "artifacts"), ("◀️ بازگشت", "home")]]))
    except Exception as e:
        send(chat_id, f"❌ خطا:\n<code>{safe_text(e)}</code>", buttons([[("◀️ بازگشت", "home")]]))


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

SESSIONS: dict[int, dict[str, Any]] = {}


def session(uid: int) -> dict[str, Any]:
    return SESSIONS.setdefault(uid, {})


def clear_session(uid: int) -> None:
    SESSIONS.pop(uid, None)


def prompt_model_add(chat_id: int, uid: int) -> None:
    session(uid).update(flow="model_add", step="id", data={})
    send(chat_id, "<b>افزودن مدل — مرحله ۱/۶</b>\nیک شناسه کوتاه وارد کن؛ مثال: <code>nvidia-gptoss</code>", buttons([[("لغو", "cancel_flow")]]))


def prompt_model_edit(chat_id: int, uid: int) -> None:
    rows = model_rows()
    if not rows:
        send(chat_id, "مدلی برای ویرایش وجود ندارد.", buttons([[("◀️ بازگشت", "models")]]))
        return
    kb = [[{"text": r["id"], "callback_data": f"model:edit:{r['id']}"}] for r in rows]
    kb.append([{"text": "◀️ بازگشت", "callback_data": "models"}])
    send(chat_id, "مدل موردنظر را انتخاب کن:", kb)


def prompt_model_delete(chat_id: int, uid: int) -> None:
    rows = model_rows()
    if not rows:
        send(chat_id, "مدلی برای حذف وجود ندارد.", buttons([[("◀️ بازگشت", "models")]]))
        return
    kb = [[{"text": f"🗑 {r['id']}", "callback_data": f"model:delete:{r['id']}"}] for r in rows]
    kb.append([{"text": "◀️ بازگشت", "callback_data": "models"}])
    send(chat_id, "مدل موردنظر را انتخاب کن:", kb)


def model_edit_start(chat_id: int, uid: int, model_id: str) -> None:
    row = DB.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
    if not row:
        send(chat_id, "مدل پیدا نشد.")
        return
    sec = DB.execute("SELECT * FROM model_secrets WHERE model_id=?", (model_id,)).fetchone()
    data = model_to_dict(row)
    data.pop("api_key", None)
    if sec:
        for k in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token", "ollama_base_url"):
            data.pop(k, None)
    session(uid).update(flow="model_edit", step="name", model_id=model_id, data=data)
    send(chat_id, f"<b>ویرایش {safe_text(model_id)}</b>\nنام نمایشی جدید را بفرست. برای نگه‌داشتن مقدار قبلی <code>-</code> بفرست.", buttons([[("لغو", "cancel_flow")]]))


def handle_model_flow(chat_id: int, uid: int, text: str) -> None:
    s = session(uid)
    flow = s.get("flow")
    step = s.get("step")
    data = s.setdefault("data", {})
    value = text.strip()
    if value.lower() in {"لغو", "/cancel", "cancel"}:
        clear_session(uid)
        main_menu(chat_id)
        return

    if flow == "model_add":
        if step == "id":
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{2,40}", value):
                send(chat_id, "شناسه نامعتبر است. فقط حروف، عدد، نقطه، خط و زیرخط.")
                return
            if DB.execute("SELECT 1 FROM models WHERE id=?", (value.lower(),)).fetchone():
                send(chat_id, "این شناسه قبلاً وجود دارد.")
                return
            data["id"] = value.lower(); s["step"] = "name"
            send(chat_id, "<b>مرحله ۲/۶</b>\nنام نمایشی مدل:")
        elif step == "name":
            data["name"] = value; s["step"] = "provider"
            send(chat_id, "<b>مرحله ۳/۶</b>\nProvider را وارد کن؛ مثال: <code>openai</code> یا <code>openai_compatible</code> یا <code>nvidia</code>.")
        elif step == "provider":
            data["provider"] = value.lower(); s["step"] = "model"
            send(chat_id, "<b>مرحله ۴/۶</b>\nنام دقیق مدل:")
        elif step == "model":
            data["model"] = value; s["step"] = "base_url"
            send(chat_id, "<b>مرحله ۵/۶</b>\nBase URL را بفرست؛ اگر لازم نیست <code>-</code>.")
        elif step == "base_url":
            data["base_url"] = "" if value == "-" else value; s["step"] = "api_key"
            send(chat_id, "<b>مرحله ۶/۶</b>\nAPI Key را بفرست. این مقدار در پیام دیگری نمایش داده نمی‌شود و فقط در Secret مدل ذخیره می‌شود.")
        elif step == "api_key":
            data["api_key"] = value
            try:
                add_or_update_model(data)
                clear_session(uid)
                send(chat_id, "✅ مدل ثبت و Secret مدل در GitHub به‌روزرسانی شد.", buttons([[("🤖 مدل‌ها", "models"), ("◀️ خانه", "home")]]))
            except Exception as e:
                send(chat_id, f"❌ ذخیره مدل انجام نشد:\n<code>{safe_text(e)}</code>")
    elif flow == "model_edit":
        if step == "name":
            if value != "-": data["name"] = value
            s["step"] = "provider"
            send(chat_id, "Provider جدید یا <code>-</code> برای حفظ قبلی:")
        elif step == "provider":
            if value != "-": data["provider"] = value.lower()
            s["step"] = "model"
            send(chat_id, "نام مدل جدید یا <code>-</code> برای حفظ قبلی:")
        elif step == "model":
            if value != "-": data["model"] = value
            s["step"] = "base_url"
            send(chat_id, "Base URL جدید یا <code>-</code> برای حفظ قبلی:")
        elif step == "base_url":
            if value != "-": data["base_url"] = value
            s["step"] = "api_key"
            send(chat_id, "API Key جدید یا <code>-</code> برای حفظ قبلی:")
        elif step == "api_key":
            if value != "-": data["api_key"] = value
            else:
                old = DB.execute("SELECT api_key FROM model_secrets WHERE model_id=?", (s["model_id"],)).fetchone()
                data["api_key"] = old[0] if old else ""
            try:
                add_or_update_model(data)
                clear_session(uid)
                send(chat_id, "✅ مدل و Secret آن به‌روزرسانی شد.", buttons([[("🤖 مدل‌ها", "models"), ("◀️ خانه", "home")]]))
            except Exception as e:
                send(chat_id, f"❌ خطا:\n<code>{safe_text(e)}</code>")


def prompt_analysis(chat_id: int, uid: int) -> None:
    session(uid).update(flow="analysis", step="ticker", data={})
    send(chat_id, "<b>تحلیل — مرحله ۱/۴</b>\nTicker دقیق را وارد کن؛ مثال <code>NVDA</code> یا <code>0700.HK</code>.", buttons([[("لغو", "cancel_flow")]]))


def handle_analysis_flow(chat_id: int, uid: int, text: str) -> None:
    s = session(uid); step = s.get("step"); data = s.setdefault("data", {}); value = text.strip()
    if value.lower() in {"لغو", "/cancel", "cancel"}:
        clear_session(uid); main_menu(chat_id); return
    if step == "ticker":
        if not re.fullmatch(r"[A-Za-z0-9.^_-]{1,32}", value):
            send(chat_id, "Ticker نامعتبر است."); return
        data["ticker"] = value.upper(); s["step"] = "date"
        send(chat_id, "<b>مرحله ۲/۴</b>\nتاریخ تحلیل YYYY-MM-DD:")
    elif step == "date":
        try: dt.date.fromisoformat(value)
        except ValueError: send(chat_id, "تاریخ باید به شکل YYYY-MM-DD باشد."); return
        data["date"] = value; s["step"] = "analysts"
        send(chat_id, "<b>مرحله ۳/۴</b>\nتحلیل‌گرها را وارد کن یا <code>all</code>.\nمثال: <code>market,social,news,fundamentals</code>")
    elif step == "analysts":
        allowed = {"market", "social", "news", "fundamentals"}
        chosen = [x.strip().lower() for x in value.split(",") if x.strip()]
        if value.lower() == "all": chosen = sorted(allowed)
        if not chosen or any(x not in allowed for x in chosen):
            send(chat_id, "تحلیل‌گر نامعتبر است. گزینه‌ها: market, social, news, fundamentals."); return
        data["analysts"] = ",".join(dict.fromkeys(chosen)); s["step"] = "model"
        rows = model_rows()
        if not rows:
            send(chat_id, "هیچ مدلی ثبت نشده. ابتدا یک مدل اضافه کن.", buttons([[("🤖 مدل‌ها", "models")]])); clear_session(uid); return
        kb = [[{"text": f"{r['name']} ({r['id']})", "callback_data": f"pickrun:{r['id']}"}] for r in rows]
        kb.append([{"text": "لغو", "callback_data": "cancel_flow"}])
        send(chat_id, "<b>مرحله ۴/۴</b>\nمدل را انتخاب کن:", kb)


def launch_analysis(chat_id: int, uid: int, model_id: str) -> None:
    s = session(uid); data = s.get("data", {})
    request_id = uuid.uuid4().hex
    payload = {
        "mode": "analyze",
        "ticker": data["ticker"],
        "date": data["date"],
        "analysts": data["analysts"],
        "asset_type": "stock",
        "portfolio_json": "",
        "start": "",
        "end": "",
        "every": "7",
        "run_id": "",
        "model_id": model_id,
        "output_language": db_setting("output_language", "English"),
        "max_debate_rounds": "",
        "max_risk_rounds": "",
        "max_tool_rounds": "",
        "llm_max_retries": "",
        "max_tokens": "",
        "temperature": "",
        "openai_reasoning_effort": "",
        "google_thinking_level": "",
        "anthropic_effort": "",
        "checkpoint": "true",
        "clear_checkpoints": "false",
        "request_id": request_id,
    }
    try:
        dispatch(payload)
        db_execute(
            "INSERT INTO runs(request_id,mode, ticker,date_or_start,status,model_id,analysts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (request_id, "analyze", data["ticker"], data["date"], "queued", model_id, data["analysts"], now(), now()),
        )
        clear_session(uid)
        send(chat_id, f"✅ اجرا ارسال شد.\nRequest ID: <code>{request_id}</code>\nTicker: <b>{safe_text(data['ticker'])}</b>\nModel: <b>{safe_text(model_id)}</b>", buttons([[("📊 وضعیت", "status"), ("📜 تاریخچه", "history")]]))
    except Exception as e:
        send(chat_id, f"❌ Dispatch ناموفق بود:\n<code>{safe_text(e)}</code>")


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

OFFSET = 0


def handle_message(msg: dict[str, Any]) -> None:
    global OFFSET
    user = msg.get("from") or {}
    uid = int(user.get("id", 0))
    chat = msg.get("chat") or {}
    chat_id = int(chat.get("id", uid))
    remember_user(user)
    if not authorized(uid):
        send(chat_id, "⛔ دسترسی مجاز نیست.")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in {"/start", "/menu"}:
            clear_session(uid); main_menu(chat_id); return
        if cmd == "/status": status_menu(chat_id); return
        if cmd == "/models": model_menu(chat_id); return
        if cmd == "/cancel": clear_session(uid); main_menu(chat_id); return
        if cmd == "/testmodel": test_models_menu(chat_id); return
        if cmd == "/run": run_menu(chat_id); return

    if uid in SESSIONS and SESSIONS[uid].get("flow"):
        flow = SESSIONS[uid]["flow"]
        if flow.startswith("model_"):
            handle_model_flow(chat_id, uid, text)
        elif flow == "analysis":
            handle_analysis_flow(chat_id, uid, text)


def handle_callback(q: dict[str, Any]) -> None:
    uid = int((q.get("from") or {}).get("id", 0))
    msg = q.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id", uid))
    data = q.get("data", "")
    if not authorized(uid):
        answer_callback(q["id"], "دسترسی مجاز نیست.", alert=True); return
    answer_callback(q["id"])

    if data == "home": main_menu(chat_id); return
    if data == "status": status_menu(chat_id); return
    if data == "models": model_menu(chat_id); return
    if data == "history": history_menu(chat_id); return
    if data == "artifacts": artifacts_menu(chat_id); return
    if data == "settings": settings_menu(chat_id); return
    if data == "run:menu": run_menu(chat_id); return
    if data == "run:analyze": prompt_analysis(chat_id, uid); return
    if data == "run:backtest":
        send(chat_id, "برای بک‌تست فعلاً از اجرای مستقیم Workflow استفاده کن؛ فرم کامل آن در نسخه بعدی کنترلر اضافه می‌شود.", buttons([[("◀️ بازگشت", "run:menu")]])); return
    if data == "cancel_flow": clear_session(uid); main_menu(chat_id); return
    if data == "model:add": prompt_model_add(chat_id, uid); return
    if data == "model:edit": prompt_model_edit(chat_id, uid); return
    if data == "model:delete": prompt_model_delete(chat_id, uid); return
    if data.startswith("model:edit:"):
        model_edit_start(chat_id, uid, data.split(":", 2)[2]); return
    if data.startswith("model:delete:"):
        mid = data.split(":", 2)[2]
        if not is_admin(uid):
            send(chat_id, "⛔ حذف مدل فقط برای Admin است."); return
        try:
            delete_model(mid); send(chat_id, "✅ مدل حذف شد و Catalog در GitHub به‌روزرسانی شد.")
        except Exception as e:
            send(chat_id, f"❌ حذف ناموفق:\n<code>{safe_text(e)}</code>")
        model_menu(chat_id); return
    if data.startswith("pickrun:"):
        launch_analysis(chat_id, uid, data.split(":", 1)[1]); return
    if data == "testmodels": test_models_menu(chat_id); return
    if data.startswith("test:"):
        mid = data.split(":", 1)[1]
        launch_test(chat_id, uid, mid); return
    if data == "preset:full":
        set_setting("output_language", "English"); send(chat_id, "✅ حالت کامل انتخاب شد."); return
    if data == "preset:quick":
        set_setting("output_language", "English"); send(chat_id, "✅ حالت سریع انتخاب شد."); return
    if data == "cancel:menu": cancel_menu(chat_id); return


def test_models_menu(chat_id: int) -> None:
    rows = model_rows()
    if not rows:
        send(chat_id, "مدلی ثبت نشده است.", buttons([[("🤖 مدل‌ها", "models")]])); return
    kb = [[{"text": f"🧪 {r['name']} ({r['id']})", "callback_data": f"test:{r['id']}"}] for r in rows]
    kb.append([{"text": "◀️ بازگشت", "callback_data": "home"}])
    send(chat_id, "تست مدل یک اجرای واقعی سبک روی <code>AAPL</code> می‌فرستد؛ ممکن است مصرف API داشته باشد.", kb)


def launch_test(chat_id: int, uid: int, model_id: str) -> None:
    request_id = uuid.uuid4().hex
    try:
        dispatch({
            "mode": "analyze", "ticker": "AAPL", "date": dt.date.today().isoformat(),
            "analysts": "market", "asset_type": "stock", "portfolio_json": "", "start": "", "end": "",
            "every": "7", "run_id": "", "model_id": model_id, "output_language": "English",
            "max_debate_rounds": "1", "max_risk_rounds": "1", "max_tool_rounds": "3",
            "llm_max_retries": "1", "max_tokens": "500", "temperature": "0", "openai_reasoning_effort": "",
            "google_thinking_level": "", "anthropic_effort": "", "checkpoint": "false",
            "clear_checkpoints": "false", "request_id": request_id,
        })
        db_execute("INSERT INTO runs(request_id,mode,ticker,date_or_start,status,model_id,analysts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (request_id, "model_test", "AAPL", dt.date.today().isoformat(), "queued", model_id, "market", now(), now()))
        send(chat_id, f"🧪 تست مدل ارسال شد.\nModel: <b>{safe_text(model_id)}</b>\nRequest: <code>{request_id}</code>", buttons([[("📊 وضعیت", "status")]]))
    except Exception as e:
        send(chat_id, f"❌ تست ارسال نشد:\n<code>{safe_text(e)}</code>")


def cancel_menu(chat_id: int) -> None:
    try:
        runs = [r for r in list_runs(20) if r.get("status") in {"queued", "in_progress", "waiting", "requested"}]
        if not runs:
            send(chat_id, "اجرای فعالی پیدا نشد.", buttons([[("◀️ بازگشت", "home")]])); return
        kb = [[{"text": f"⏹ {r.get('id')} — {r.get('display_title') or r.get('name')}", "callback_data": f"cancel:{r.get('id')}"}] for r in runs[:10]]
        kb.append([{"text": "◀️ بازگشت", "callback_data": "home"}])
        send(chat_id, "اجرای موردنظر را برای توقف انتخاب کن:", kb)
    except Exception as e:
        send(chat_id, f"❌ خطا:\n<code>{safe_text(e)}</code>")


# Extend callback handling for cancellation without duplicating the dispatcher.
_old_handle_callback = handle_callback

def handle_callback(q: dict[str, Any]) -> None:  # noqa: F811
    data = q.get("data", "")
    if data.startswith("cancel:") and data != "cancel:menu":
        uid = int((q.get("from") or {}).get("id", 0)); chat_id = int(((q.get("message") or {}).get("chat") or {}).get("id", uid))
        if not authorized(uid):
            answer_callback(q["id"], "دسترسی مجاز نیست.", alert=True); return
        answer_callback(q["id"])
        try:
            cancel_run(int(data.split(":", 1)[1]))
            send(chat_id, "⏹ درخواست توقف ارسال شد.", buttons([[("📊 وضعیت", "status"), ("◀️ خانه", "home")]]))
        except Exception as e:
            send(chat_id, f"❌ توقف ناموفق:\n<code>{safe_text(e)}</code>")
        return
    _old_handle_callback(q)


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

def poll() -> None:
    global OFFSET
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS must be configured")

    me = tg("getMe")
    print(f"TradingAgents Telegram Controller started as @{me.get('username', '')}", flush=True)
    while True:
        try:
            updates = tg("getUpdates", {"offset": OFFSET, "timeout": 50, "allowed_updates": ["message", "callback_query"]})
            for update in updates or []:
                OFFSET = max(OFFSET, int(update["update_id"]) + 1)
                try:
                    if "message" in update:
                        handle_message(update["message"])
                    elif "callback_query" in update:
                        handle_callback(update["callback_query"])
                except Exception:
                    traceback.print_exc()
                    try:
                        chat_id = int(((update.get("message") or update.get("callback_query", {}).get("message") or {}).get("chat") or {}).get("id", 0))
                        if chat_id:
                            send(chat_id, "❌ یک خطای داخلی رخ داد. جزئیات در پیام تلگرام نمایش داده نمی‌شود.")
                    except Exception:
                        pass
        except Exception as e:
            print(f"Polling error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    poll()
