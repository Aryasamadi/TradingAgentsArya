#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-file Telegram controller for TradingAgentsArya.

The bot is the control plane. GitHub Actions is the execution plane.
Models are added/activated from Telegram and their credentials are stored as
GitHub repository secrets. No model setup is exposed in the workflow UI.
"""
from __future__ import annotations

import base64
import contextlib
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
ADMIN_IDS = {
    int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
ALLOWED_IDS = {
    int(x)
    for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH = f"https://api.github.com/repos/{OWNER}/{REPO}"

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript(
    """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS models(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 base_url TEXT DEFAULT '', region TEXT DEFAULT '', api_key TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
 request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, ticker TEXT NOT NULL, date TEXT NOT NULL,
 analysts TEXT DEFAULT '', model_id TEXT DEFAULT '', status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""
)
DB.commit()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def http_json(
    url: str,
    method: str = "GET",
    data: Any = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    request_headers = {"Accept": "application/vnd.github+json"}
    if headers:
        request_headers.update(headers)
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
    status, obj = http_json(
        f"{TG}/{method}",
        "POST",
        payload or {},
        {"Content-Type": "application/json"},
    )
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API error: {obj}")
    return obj.get("result")


def gh(method: str, path: str, data: Any = None):
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
    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
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
    return tg("editMessageText", payload)


def cb(query_id: str, text: str = "", alert: bool = False):
    tg(
        "answerCallbackQuery",
        {"callback_query_id": query_id, "text": text[:200], "show_alert": alert},
    )


def kb(rows):
    return [[{"text": label, "callback_data": data} for label, data in row] for row in rows]


def authorized(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in ALLOWED_IDS


def admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def model_rows():
    return DB.execute(
        "SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE"
    ).fetchall()


def active_model_id() -> str:
    row = DB.execute(
        "SELECT value FROM settings WHERE key='active_model'"
    ).fetchone()
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
    except ImportError as err:
        raise RuntimeError(
            "PyNaCl is required for model-secret management. "
            "Install pynacl in the bot environment."
        ) from err

    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode())
    gh(
        "PUT",
        f"/actions/secrets/{urllib.parse.quote(name, safe='')}",
        {
            "encrypted_value": base64.b64encode(encrypted).decode(),
            "key_id": key["key_id"],
        },
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


def activate_model(model_id: str):
    row = DB.execute(
        "SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)
    ).fetchone()
    if not row:
        raise ValueError("مدل پیدا نشد.")

    provider = row["provider"].lower().strip()
    secret = PROVIDER_SECRET.get(provider)
    if provider not in PROVIDER_SECRET and provider not in NO_KEY_PROVIDERS:
        raise ValueError(f"Provider پشتیبانی‌شده نیست: {provider}")

    if secret:
        if not row["api_key"]:
            raise ValueError("API Key این مدل ثبت نشده است.")
        github_secret(secret, row["api_key"])

    if provider == "ollama":
        github_secret(
            "OLLAMA_BASE_URL",
            row["base_url"] or "http://localhost:11434/v1",
        )
    elif provider == "bedrock":
        if not row["region"]:
            raise ValueError("برای Bedrock باید region ثبت شود.")
        github_secret("AWS_DEFAULT_REGION", row["region"])

    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", row["model"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", row["model"])
    github_secret(
        "TRADINGAGENTS_LLM_BACKEND_URL",
        row["base_url"] if provider != "ollama" else "",
    )
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")
    set_active(model_id)
    return row


def dispatch_run(
    mode: str,
    *,
    ticker: str = "",
    date: str = "",
    tickers: str = "",
    start: str = "",
    end: str = "",
    every: int = 7,
    analysts: str = "",
    portfolio_json: str = "",
    run_id: str = "",
):
    if not active_model_id():
        raise ValueError(
            "هنوز هیچ مدل فعالی ثبت نشده است. "
            "اول از بخش مدل‌ها یک مدل اضافه و فعال کن."
        )

    model_id = active_model_id()
    activate_model(model_id)
    request_id = uuid.uuid4().hex
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
        "run_id": run_id.strip(),
    }
    gh(
        "POST",
        "/dispatches",
        {"event_type": "tradingagents_run", "client_payload": payload},
    )

    display_ticker = payload["ticker"] or payload["tickers"]
    display_date = payload["date"] or f"{payload['start']} → {payload['end']}"
    db(
        "INSERT INTO runs(request_id,ticker,date,analysts,model_id,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            request_id,
            display_ticker,
            display_date,
            payload["analysts"],
            model_id,
            "queued",
            now(),
            now(),
        ),
    )
    return request_id, model_id


def list_runs():
    data = gh("GET", "/actions/runs?per_page=15")
    return data.get("workflow_runs", []) if isinstance(data, dict) else []


def main_menu(chat_id: int):
    send(
        chat_id,
        "<b>TradingAgents</b>\nکنترلر آماده است.",
        kb(
            [
                [("▶️ اجرای تحلیل", "run"), ("📊 وضعیت", "status")],
                [("🤖 مدل‌ها", "models"), ("🧪 تست مدل", "tests")],
                [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")],
            ]
        ),
    )


def model_menu(chat_id: int):
    rows = model_rows()
    active = active_model_id()
    text = "<b>مدل‌ها</b>\n"
    if not rows:
        text += "هنوز مدلی ثبت نشده است."
    for row in rows:
        mark = " ✅" if row["id"] == active else ""
        text += (
            f"• <code>{esc(row['id'])}</code> — {esc(row['name'])} — "
            f"{esc(row['provider'])}/{esc(row['model'])}{mark}\n"
        )

    rows_kb = [[("➕ افزودن مدل", "add")]]
    for row in rows:
        rows_kb.append(
            [(f"▶️ فعال: {row['name']}", f"activate:{row['id']}")]
        )
    rows_kb += [[("🗑 حذف مدل", "delmenu")], [("◀️ خانه", "home")]]
    send(chat_id, text, kb(rows_kb))


def run_menu(chat_id: int):
    if not model_rows():
        send(
            chat_id,
            "⚠️ ابتدا از بخش <b>مدل‌ها</b> یک مدل اضافه و فعال کن.",
            kb([[('🤖 مدل‌ها', 'models')]]),
        )
        return

    text = (
        "<b>اجرای تحلیل</b>\n"
        "تحلیل تکی: <code>/analyze NVDA 2026-09-30</code>\n"
        "بک‌تست: <code>/backtest NVDA,AAPL 2026-01-01 2026-09-30 7</code>\n\n"
        "تحلیل‌گرهای خاص را می‌توانی در انتهای دستور بنویسی: "
        "<code>market,news,fundamentals,social</code>"
    )
    send(chat_id, text, kb([[('◀️ خانه', 'home')]]))


def status(chat_id: int):
    runs = list_runs()
    lines = ["<b>وضعیت اجراها</b>"]
    for run in runs[:8]:
        lines.append(
            f"• <code>{run.get('id')}</code> — "
            f"{esc(run.get('status'))}/{esc(run.get('conclusion') or 'running')}"
        )
    send(
        chat_id,
        "\n".join(lines),
        kb([[('🔄 تازه‌سازی', 'status'), ('◀️ خانه', 'home')]]),
    )


def history(chat_id: int):
    rows = DB.execute(
        "SELECT * FROM runs ORDER BY created_at DESC LIMIT 12"
    ).fetchall()
    if not rows:
        text = "<b>تاریخچه</b>\nهنوز اجرا نداریم."
    else:
        text = "<b>تاریخچه</b>\n" + "\n".join(
            f"• <code>{esc(row['request_id'][:10])}</code> "
            f"{esc(row['ticker'])} — {esc(row['status'])}"
            for row in rows
        )
    send(chat_id, text, kb([[('◀️ خانه', 'home')]]))


def artifacts(chat_id: int):
    data = gh("GET", "/actions/artifacts?per_page=20")
    artifacts_data = data.get("artifacts", []) if isinstance(data, dict) else []
    if not artifacts_data:
        text = "<b>Artifactها</b>\nموردی نیست."
    else:
        text = "<b>Artifactها</b>\n" + "\n".join(
            f"• <code>{artifact.get('id')}</code> — {esc(artifact.get('name'))}"
            for artifact in artifacts_data[:12]
        )
    send(
        chat_id,
        text,
        kb([[('🔄 تازه‌سازی', 'artifacts'), ('◀️ خانه', 'home')]]),
    )


def add_model_prompt(chat_id: int):
    send(
        chat_id,
        "مدل را در یک پیام JSON بفرست:\n"
        '<code>{"id":"nvidia","name":"NVIDIA GPT",'
        '"provider":"nvidia","model":"openai/gpt-oss-20b",'
        '"base_url":"","api_key":"YOUR_KEY"}</code>\n\n'
        "API Key فقط برای ذخیره در GitHub Secret استفاده می‌شود و "
        "در پیام‌های بعدی نمایش داده نمی‌شود.",
        kb([[('◀️ مدل‌ها', 'models')]]),
    )


def handle_command(message: dict):
    chat_id = int(message["chat"]["id"])
    user_id = int(message["from"]["id"])
    text = (message.get("text") or "").strip()

    if text.startswith("/start"):
        main_menu(chat_id)
        return

    if text.startswith("/analyze"):
        parts = text.split()
        if len(parts) < 3:
            send(
                chat_id,
                "فرمت: <code>/analyze TICKER YYYY-MM-DD [analysts]</code>",
            )
            return
        try:
            request_id, model_id = dispatch_run(
                "analysis",
                ticker=parts[1],
                date=parts[2],
                analysts=parts[3] if len(parts) > 3 else "",
            )
            send(
                chat_id,
                f"✅ تحلیل ارسال شد.\nمدل: <code>{esc(model_id)}</code>\n"
                f"Request: <code>{request_id}</code>",
            )
        except Exception as err:
            send(chat_id, f"❌ {esc(err)}")
        return

    if text.startswith("/backtest"):
        parts = text.split()
        if len(parts) < 4:
            send(
                chat_id,
                "فرمت: <code>/backtest TICKERS START END [every] [analysts]</code>",
            )
            return
        try:
            every = int(parts[4]) if len(parts) > 4 else 7
            analysts = parts[5] if len(parts) > 5 else ""
            request_id, model_id = dispatch_run(
                "backtest",
                tickers=parts[1],
                start=parts[2],
                end=parts[3],
                every=every,
                analysts=analysts,
            )
            send(
                chat_id,
                f"✅ بک‌تست ارسال شد.\nمدل: <code>{esc(model_id)}</code>\n"
                f"Request: <code>{request_id}</code>",
            )
        except Exception as err:
            send(chat_id, f"❌ {esc(err)}")
        return

    if text.startswith("{") and admin(user_id):
        try:
            data = json.loads(text)
            with contextlib.suppress(Exception):
                tg(
                    "deleteMessage",
                    {
                        "chat_id": chat_id,
                        "message_id": int(message.get("message_id", 0)),
                    },
                )

            required = ["id", "name", "provider", "model"]
            if any(not str(data.get(key, "")).strip() for key in required):
                raise ValueError("id/name/provider/model الزامی است.")

            timestamp = now()
            db(
                "INSERT INTO models(id,name,provider,model,base_url,region,api_key,enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "name=excluded.name,provider=excluded.provider,model=excluded.model,"
                "base_url=excluded.base_url,region=excluded.region,api_key=excluded.api_key,"
                "enabled=1,updated_at=excluded.updated_at",
                (
                    data["id"],
                    data["name"],
                    data["provider"],
                    data["model"],
                    data.get("base_url", ""),
                    data.get("region", ""),
                    data.get("api_key", ""),
                    timestamp,
                    timestamp,
                ),
            )
            activate_model(data["id"])
            send(
                chat_id,
                "✅ مدل ثبت و فعال شد. تنظیمات امن آن در GitHub Secrets قرار گرفت.",
                kb(
                    [
                        [("▶️ اجرای تحلیل", "run"), ("🧪 تست مدل", "tests")],
                        [("🤖 مدل‌ها", "models")],
                    ]
                ),
            )
        except Exception as err:
            send(
                chat_id,
                f"❌ JSON مدل نامعتبر است:\n<code>{esc(err)}</code>",
            )


def test_menu(chat_id: int):
    rows = model_rows()
    if not rows:
        send(
            chat_id,
            "⚠️ هنوز مدلی ثبت نشده است.",
            kb([[('🤖 مدل‌ها', 'models')]]),
        )
        return
    rows_kb = [
        [(f"🧪 تست {row['name']}", f"test:{row['id']}")] for row in rows
    ]
    rows_kb.append([('◀️ خانه', 'home')])
    send(chat_id, "مدل موردنظر برای تست را انتخاب کن:", kb(rows_kb))


def launch_test(chat_id: int, model_id: str):
    try:
        set_active(model_id)
        request_id, active = dispatch_run(
            "analysis",
            ticker="AAPL",
            date=dt.date.today().isoformat(),
            analysts="market",
        )
        send(
            chat_id,
            f"🧪 تست ارسال شد.\nمدل: <code>{esc(active)}</code>\n"
            f"Request: <code>{request_id}</code>",
            kb([[('📊 وضعیت', 'status'), ('◀️ خانه', 'home')]]),
        )
    except Exception as err:
        send(
            chat_id,
            f"❌ تست ناموفق بود:\n<code>{esc(err)}</code>",
            kb([[('🤖 مدل‌ها', 'models')]]),
        )


def callback(query: dict):
    user_id = int(query.get("from", {}).get("id", 0))
    chat_id = int(
        query.get("message", {}).get("chat", {}).get("id", user_id)
    )
    data = query.get("data", "")

    if not authorized(user_id):
        cb(query["id"], "دسترسی مجاز نیست.", True)
        return

    cb(query["id"])
    if data == "home":
        main_menu(chat_id)
    elif data == "models":
        model_menu(chat_id)
    elif data == "add":
        add_model_prompt(chat_id)
    elif data.startswith("activate:"):
        if not admin(user_id):
            send(chat_id, "فقط ادمین می‌تواند مدل را فعال کند.")
            return
        try:
            activate_model(data.split(":", 1)[1])
            send(
                chat_id,
                "✅ مدل فعال شد و تنظیمات آن در GitHub Secrets قرار گرفت.",
                kb([[('▶️ اجرای تحلیل', 'run'), ('◀️ خانه', 'home')]]),
            )
        except Exception as err:
            send(
                chat_id,
                f"❌ فعال‌سازی ناموفق:\n<code>{esc(err)}</code>",
            )
    elif data == "run":
        run_menu(chat_id)
    elif data == "status":
        status(chat_id)
    elif data == "history":
        history(chat_id)
    elif data == "artifacts":
        artifacts(chat_id)
    elif data == "tests":
        test_menu(chat_id)
    elif data.startswith("test:"):
        if not admin(user_id):
            send(chat_id, "⛔ فقط Admin می‌تواند تست مدل اجرا کند.")
            return
        launch_test(chat_id, data.split(":", 1)[1])
    elif data == "delmenu":
        rows = model_rows()
        delete_rows = [
            [(f"🗑 {row['name']}", f"delete:{row['id']}")] for row in rows
        ]
        delete_rows.append([('◀️ مدل‌ها', 'models')])
        send(chat_id, "مدل را برای حذف انتخاب کن.", kb(delete_rows))
    elif data.startswith("delete:"):
        if not admin(user_id):
            return
        model_id = data.split(":", 1)[1]
        db("DELETE FROM models WHERE id=?", (model_id,))
        if active_model_id() == model_id:
            db("DELETE FROM settings WHERE key='active_model'")
        send(chat_id, "✅ مدل حذف شد.", kb([[('🤖 مدل‌ها', 'models')]]))


def poll():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit(
            "TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required"
        )

    offset = 0
    me = tg("getMe")
    print(
        f"TradingAgents bot started @{me.get('username', '')}",
        flush=True,
    )
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
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    if "callback_query" in update:
                        callback(update["callback_query"])
                    elif "message" in update and authorized(
                        int(update["message"].get("from", {}).get("id", 0))
                    ):
                        handle_command(update["message"])
                except Exception:
                    traceback.print_exc()
        except Exception as err:
            print(f"Polling error: {err}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    poll()
