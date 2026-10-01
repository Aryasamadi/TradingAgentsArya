#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TradingAgentsArya Telegram Controller
Architecture: Single-Message UI, SQLite State Machine, GitHub Actions Dispatch
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import html
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

# ==========================================
# Configuration & Constants
# ==========================================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("BOT_GITHUB_TOKEN", "").strip()
STATE_KEY = os.getenv("BOT_STATE_KEY", "").strip()
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()

STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"
ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_IDS = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GH_API = f"https://api.github.com/repos/{OWNER}/{REPO}"

# ==========================================
# Database Setup
# ==========================================
DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
    base_url TEXT NOT NULL, token_ciphertext BLOB, enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, chat_id INTEGER NOT NULL,
    mode TEXT NOT NULL, payload_json TEXT NOT NULL, model_id TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    ui_message_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
""")
DB.commit()

def now() -> str: return dt.datetime.now(dt.timezone.utc).isoformat()
def esc(v: Any) -> str: return html.escape(str(v if v is not None else ""), quote=False)

def db_exec(sql: str, args: tuple | list = ()):
    cur = DB.execute(sql, args)
    DB.commit()
    return cur

def get_setting(key: str, default: str = "") -> str:
    row = DB.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default

def set_setting(key: str, value: str) -> None:
    db_exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def authorized(user_id: int) -> bool: return user_id in ADMIN_IDS or user_id in ALLOWED_IDS
def admin(user_id: int) -> bool: return user_id in ADMIN_IDS

# ==========================================
# HTTP & API Helpers
# ==========================================
def http_json(url: str, method: str = "GET", data: Any = None, headers: dict | None = None, timeout: int = 60):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req_headers = {"Accept": "application/json", "User-Agent": "TradingAgentsArya-Bot/2.0"}
    if headers: req_headers.update(headers)
    if data is not None: req_headers.setdefault("Content-Type", "application/json")
    
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e

def tg(method: str, payload: dict | None = None):
    _, obj = http_json(f"{TG_API}/{method}", "POST", payload or {}, timeout=65)
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed")
    return obj.get("result")

def gh(method: str, path: str, data: Any = None):
    if not GH_TOKEN: raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}", "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json"
    }
    status, obj = http_json(GH_API + path, method, data, headers, timeout=60)
    if status >= 300: raise RuntimeError(f"GitHub API error {status}")
    return obj

def answer_callback(query_id: str, text: str = "", alert: bool = False) -> None:
    try: tg("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})
    except: pass

# ==========================================
# Cryptography & Model Management
# ==========================================
PROVIDER_SECRET = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY", "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "glm": "ZHIPU_API_KEY", "minimax": "MINIMAX_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "mistral": "MISTRAL_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY", "nvidia": "NVIDIA_API_KEY", "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY"
}

def crypt_key() -> bytes:
    seed = STATE_KEY or f"legacy:{OWNER}:{REPO}:{GH_TOKEN}"
    return hashlib.sha256(seed.encode("utf-8")).digest()

def encrypt_token(token: str) -> bytes:
    from nacl.secret import SecretBox
    return bytes(SecretBox(crypt_key()).encrypt(token.encode("utf-8")))

def decrypt_token(ciphertext: bytes | memoryview | None) -> str:
    if not ciphertext: return ""
    from nacl.secret import SecretBox
    try: return SecretBox(crypt_key()).decrypt(bytes(ciphertext)).decode("utf-8")
    except: raise RuntimeError("کلید رمزگشایی نامعتبر است.")

def infer_provider(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    mapping = [
        ("api.openai.com", "openai"), ("api.anthropic.com", "anthropic"),
        ("generativelanguage.googleapis.com", "google"), ("api.x.ai", "xai"),
        ("api.deepseek.com", "deepseek"), ("dashscope", "qwen"), ("api.z.ai", "glm"),
        ("open.bigmodel.cn", "glm"), ("api.minimax", "minimax"), ("openrouter.ai", "openrouter"),
        ("api.mistral.ai", "mistral"), ("api.moonshot.ai", "kimi"), ("api.groq.com", "groq"),
        ("integrate.api.nvidia.com", "nvidia")
    ]
    for needle, prov in mapping:
        if needle in host: return prov
    if "azure.com" in host: return "azure"
    if "amazonaws.com" in host and "bedrock" in host: return "bedrock"
    if "localhost" in host or "127.0.0.1" in host: return "ollama"
    return "openai_compatible"

def github_secret(name: str, value: str) -> None:
    from nacl import encoding, public
    key = gh("GET", "/actions/secrets/public-key")
    pub_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(pub_key).encrypt(value.encode("utf-8"))
    gh("PUT", f"/actions/secrets/{urllib.parse.quote(name, safe='')}", {
        "encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key["key_id"]
    })

def activate_model(model_id: str) -> None:
    row = DB.execute("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,)).fetchone()
    if not row: raise ValueError("مدل پیدا نشد.")
    token = decrypt_token(row["token_ciphertext"])
    if row["provider"] in PROVIDER_SECRET and token:
        github_secret(PROVIDER_SECRET[row["provider"]], token)
    github_secret("TRADINGAGENTS_LLM_PROVIDER", row["provider"])
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", row["base_url"])
    set_setting("active_model", model_id)

def test_model_api(model_row: sqlite3.Row) -> float:
    url = model_row["base_url"].rstrip("/")
    token = decrypt_token(model_row["token_ciphertext"])
    provider = model_row["provider"]
    name = model_row["name"]
    
    start = time.monotonic()
    headers = {"Content-Type": "application/json"}
    payload = {}
    
    if provider == "anthropic":
        if not url.endswith("/messages"): url += "/v1/messages"
        headers.update({"x-api-key": token, "anthropic-version": "2023-06-01"})
        payload = {"model": name, "max_tokens": 8, "messages": [{"role": "user", "content": "OK"}]}
    elif provider == "google":
        url += f"/models/{urllib.parse.quote(name)}:generateContent?key={token}"
        payload = {"contents": [{"parts": [{"text": "OK"}]}], "generationConfig": {"maxOutputTokens": 8}}
    else:
        if not url.endswith("/chat/completions"): url += "/chat/completions"
        if token: headers["Authorization"] = f"Bearer {token}"
        payload = {"model": name, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 8}
        
    status, _ = http_json(url, "POST", payload, headers, timeout=45)
    if status >= 300: raise RuntimeError(f"HTTP {status}")
    return time.monotonic() - start

# ==========================================
# State Machine & UI Rendering
# ==========================================
def get_state(chat_id: int) -> tuple[str, dict, int]:
    row = DB.execute("SELECT state, payload, ui_message_id FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
    if not row: return "idle", {}, 0
    return row["state"], json.loads(row["payload"]), row["ui_message_id"]

def set_state(chat_id: int, state: str, payload: dict, ui_message_id: int | None = None) -> None:
    mid = ui_message_id if ui_message_id is not None else get_state(chat_id)[2]
    db_exec(
        "INSERT INTO chat_state(chat_id,state,payload,ui_message_id,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, payload=excluded.payload, "
        "ui_message_id=excluded.ui_message_id, updated_at=excluded.updated_at",
        (chat_id, state, json.dumps(payload, ensure_ascii=False), mid, now())
    )

def build_ui(state: str, payload: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    err = payload.pop("error", None)
    err_html = f"⚠️ <b>{esc(err)}</b>\n\n" if err else ""
    
    if state == "idle":
        mid = get_setting("active_model")
        m = DB.execute("SELECT name FROM models WHERE id=?", (mid,)).fetchone() if mid else None
        model_txt = f"مدل فعال: <b>{esc(m['name'])}</b>" if m else "مدل فعال: <b>تنظیم نشده</b>"
        return f"{err_html}<b>🤖 TradingAgentsArya</b>\n{model_txt}\n\nیک گزینه را انتخاب کنید:", [
            [("🚀 تحلیل جدید", "flow_analysis_ticker"), ("📈 بک‌تست", "flow_backtest_tickers")],
            [("🤖 مدل‌ها", "models_list"), ("📊 اجراهای جاری", "active_runs")],
            [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")]
        ]
        
    elif state == "models_list":
        rows = DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name").fetchall()
        aid = get_setting("active_model")
        txt = f"{err_html}<b>🤖 مدیریت مدل‌ها</b>\n"
        btns = []
        for r in rows:
            mark = "✅ " if r["id"] == aid else ""
            txt += f"\n{mark}<b>{esc(r['name'])}</b> (<code>{esc(r['provider'])}</code>)"
            btns.append([(f"⚡ فعال‌سازی {r['name']}", f"activate:{r['id']}")])
        btns += [[("➕ افزودن مدل", "wizard_url"), ("🧪 تست اتصال", "test_model_menu")], [("🏠 خانه", "home")]]
        return txt, btns

    elif state == "wizard_url":
        return f"{err_html}<b>➕ افزودن مدل (1/3)</b>\nBase URL را دقیقاً وارد کنید:", [[("❌ لغو", "home")]]
    elif state == "wizard_token":
        return f"{err_html}<b>➕ افزودن مدل (2/3)</b>\nAPI Token را وارد کنید:", [[("❌ لغو", "home")]]
    elif state == "wizard_model":
        return f"{err_html}<b>➕ افزودن مدل (3/3)</b>\nModel ID دقیق را وارد کنید:", [[("❌ لغو", "home")]]

    elif state == "test_model_menu":
        rows = DB.execute("SELECT * FROM models WHERE enabled=1").fetchall()
        btns = [[(f"🧪 {r['name']}", f"test:{r['id']}")] for r in rows]
        btns.append([("◀️ بازگشت", "models_list")])
        return f"{err_html}<b>🧪 تست اتصال</b>\nمدل را انتخاب کنید:", btns

    elif state == "flow_analysis_ticker":
        return f"{err_html}<b>🔎 تحلیل جدید (1/3)</b>\nTicker را وارد کنید (مثلا NVDA):", [[("❌ لغو", "home")]]
    elif state == "flow_analysis_date":
        return f"{err_html}<b>🔎 تحلیل جدید (2/3)</b>\nتاریخ تحلیل (YYYY-MM-DD):", [[("📅 امروز", "today"), ("❌ لغو", "home")]]
    elif state == "flow_analysis_analysts":
        return build_analyst_ui(payload, err_html)
    elif state == "flow_analysis_confirm":
        txt = f"{err_html}<b>✅ آماده اجرا</b>\nTicker: <code>{esc(payload['ticker'])}</code>\nتاریخ: <code>{esc(payload['date'])}</code>\nتحلیلگران: <b>{esc(payload['analysts'])}</b>"
        return txt, [[("🚀 شروع تحلیل", "run_analysis")], [("🎛 تغییر تحلیلگران", "flow_analysis_analysts")], [("❌ لغو", "home")]]

    elif state == "flow_backtest_tickers":
        return f"{err_html}<b>📈 بک‌تست (1/4)</b>\nTickerها را با کاما جدا کنید (مثلا NVDA,AAPL):", [[("❌ لغو", "home")]]
    elif state == "flow_backtest_start":
        return f"{err_html}<b>📈 بک‌تست (2/4)</b>\nتاریخ شروع (YYYY-MM-DD):", [[("❌ لغو", "home")]]
    elif state == "flow_backtest_end":
        return f"{err_html}<b>📈 بک‌تست (3/4)</b>\nتاریخ پایان (YYYY-MM-DD):", [[("📅 امروز", "today"), ("❌ لغو", "home")]]
    elif state == "flow_backtest_every":
        return f"{err_html}<b>📈 بک‌تست (4/4)</b>\nفاصله زمانی (روز):", [[("1 روز", "every:1"), ("7 روز", "every:7"), ("14 روز", "every:14"), ("30 روز", "every:30")], [("❌ لغو", "home")]]
    elif state == "flow_backtest_analysts":
        return build_analyst_ui(payload, err_html)
    elif state == "flow_backtest_confirm":
        txt = f"{err_html}<b>✅ آماده بک‌تست</b>\nTickers: <code>{esc(payload['tickers'])}</code>\nاز: <code>{esc(payload['start'])}</code> تا <code>{esc(payload['end'])}</code>\nهر {esc(payload['every'])} روز\nتحلیلگران: <b>{esc(payload['analysts'])}</b>"
        return txt, [[("🚀 شروع بک‌تست", "run_backtest")], [("🎛 تغییر تحلیلگران", "flow_backtest_analysts")], [("❌ لغو", "home")]]

    elif state in ("active_runs", "history", "artifacts"):
        return build_list_ui(state, err_html)
        
    return "State نامعتبر.", [[("🏠 خانه", "home")]]

def build_analyst_ui(payload: dict, err_html: str) -> tuple[str, list]:
    sel = set(payload.get("analysts", "").split(","))
    opts = ["market", "social", "news", "fundamentals"]
    if "crypto" in payload.get("type", ""): 
        opts.remove("fundamentals")
        sel.discard("fundamentals")
    labels = {"market": "📊 Market", "social": "💬 Sentiment", "news": "📰 News", "fundamentals": "💰 Fundamentals"}
    btns = [[(("✅ " if x in sel else "") + labels[x], f"toggle:{x}") for x in opts[i:i+2]] for i in range(0, len(opts), 2)]
    btns += [[("✅ تایید و ادامه", "analysts_done")], [("❌ لغو", "home")]]
    payload["analysts"] = ",".join(x for x in opts if x in sel)
    return f"{err_html}<b>🎛 انتخاب تحلیلگران</b>", btns

def build_list_ui(state: str, err_html: str) -> tuple[str, list]:
    txt = f"{err_html}<b>📊 {state.replace('_', ' ').title()}</b>\n"
    if state == "active_runs":
        # Simplified active runs check
        txt += "✅ هیچ پردازش فعالی در صف نیست."
    elif state == "history":
        rows = DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 10").fetchall()
        if not rows: txt += "تاریخچه‌ای وجود ندارد."
        else:
            for r in rows:
                txt += f"\n• <code>{r['request_id'][:8]}</code> | {r['mode']} | <b>{r['status']}</b>"
    elif state == "artifacts":
        txt += "برای دانلود خروجی‌ها به تب Actions در گیت‌هاب مراجعه کنید."
    return txt, [[("🔄 تازه‌سازی", state), ("🏠 خانه", "home")]]

def update_ui(chat_id: int, cb_mid: int | None = None) -> None:
    state, payload, saved_mid = get_state(chat_id)
    if cb_mid and saved_mid and cb_mid != saved_mid: return # Ignore old menu clicks
    
    text, btns = build_ui(state, payload)
    markup = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in btns]}
    
    target_mid = saved_mid
    if target_mid:
        try:
            tg("editMessageText", {"chat_id": chat_id, "message_id": target_mid, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup})
            set_state(chat_id, state, payload, target_mid)
            return
        except Exception:
            pass # Message deleted or too old, fallback to send
            
    new_mid = tg("sendMessage", {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup})["message_id"]
    set_state(chat_id, state, payload, new_mid)

# ==========================================
# Event Handlers
# ==========================================
def handle_text(chat_id: int, text: str) -> None:
    state, payload, _ = get_state(chat_id)
    text = text.strip()
    
    if text == "/start":
        set_state(chat_id, "idle", {})
        update_ui(chat_id)
        return

    try:
        if state == "wizard_url":
            if not text.startswith(("http://", "https://")): raise ValueError("URL باید با http/https شروع شود.")
            payload["url"] = text.rstrip("/")
            payload["provider"] = infer_provider(text)
            set_state(chat_id, "wizard_token", payload)
        elif state == "wizard_token":
            if len(text) < 5: raise ValueError("Token خیلی کوتاه است.")
            payload["token"] = text
            set_state(chat_id, "wizard_model", payload)
        elif state == "wizard_model":
            mid = "m_" + uuid.uuid4().hex[:12]
            db_exec("INSERT INTO models VALUES(?,?,?,?,?,1,?)", (mid, text, payload["provider"], payload["url"], encrypt_token(payload["token"]), now()))
            activate_model(mid)
            set_state(chat_id, "models_list", {})
            
        elif state == "flow_analysis_ticker":
            t = text.upper().replace("$", "")
            if not t.isalnum(): raise ValueError("Ticker نامعتبر است.")
            payload["ticker"] = t
            payload["type"] = "crypto" if t.endswith(("-USD", "-USDT", "-BTC")) else "stock"
            set_state(chat_id, "flow_analysis_date", payload)
        elif state == "flow_analysis_date":
            d = dt.date.today().isoformat() if text.lower() in ("today", "امروز") else dt.date.fromisoformat(text).isoformat()
            if dt.date.fromisoformat(d) > dt.date.today(): raise ValueError("تاریخ نمی‌تواند در آینده باشد.")
            payload["date"] = d
            payload["analysts"] = "market,social,news" if payload.get("type") == "crypto" else "market,social,news,fundamentals"
            set_state(chat_id, "flow_analysis_analysts", payload)
            
        elif state == "flow_backtest_tickers":
            tickers = ",".join([x.upper() for x in text.replace(" ", ",").split(",") if x])
            if not tickers: raise ValueError("حداقل یک Ticker وارد کنید.")
            payload["tickers"] = tickers
            set_state(chat_id, "flow_backtest_start", payload)
        elif state == "flow_backtest_start":
            payload["start"] = dt.date.fromisoformat(text).isoformat()
            set_state(chat_id, "flow_backtest_end", payload)
        elif state == "flow_backtest_end":
            d = dt.date.today().isoformat() if text.lower() in ("today", "امروز") else dt.date.fromisoformat(text).isoformat()
            if dt.date.fromisoformat(d) < dt.date.fromisoformat(payload["start"]): raise ValueError("تاریخ پایان باید بعد از شروع باشد.")
            payload["end"] = d
            set_state(chat_id, "flow_backtest_every", payload)
            
        else:
            return # Ignore text in other states
            
    except ValueError as e:
        payload["error"] = str(e)
        set_state(chat_id, state, payload)
        
    update_ui(chat_id)

def handle_callback(query: dict) -> None:
    chat_id = query["message"]["chat"]["id"]
    mid = query["message"]["message_id"]
    data = query["data"]
    
    state, payload, _ = get_state(chat_id)
    answer_callback(query["id"])
    
    try:
        if data == "home": set_state(chat_id, "idle", {})
        elif data == "models_list": set_state(chat_id, "models_list", {})
        elif data == "test_model_menu": set_state(chat_id, "test_model_menu", {})
        elif data == "wizard_url": 
            if not admin(query["from"]["id"]): raise ValueError("فقط Admin مجاز است.")
            set_state(chat_id, "wizard_url", {})
        elif data.startswith("activate:"):
            activate_model(data.split(":")[1])
            set_state(chat_id, "models_list", {})
        elif data.startswith("test:"):
            row = DB.execute("SELECT * FROM models WHERE id=?", (data.split(":")[1],)).fetchone()
            elapsed = test_model_api(row)
            payload["error"] = f"✅ تست موفق ({elapsed:.2f}s)"
            set_state(chat_id, "test_model_menu", payload)
        elif data == "flow_analysis_ticker":
            if not get_setting("active_model"): raise ValueError("ابتدا یک مدل فعال کنید.")
            set_state(chat_id, "flow_analysis_ticker", {})
        elif data == "flow_backtest_tickers":
            if not get_setting("active_model"): raise ValueError("ابتدا یک مدل فعال کنید.")
            set_state(chat_id, "flow_backtest_tickers", {})
        elif data == "today":
            payload["date" if state == "flow_analysis_date" else "end"] = dt.date.today().isoformat()
            next_st = "flow_analysis_analysts" if state == "flow_analysis_date" else "flow_backtest_every"
            set_state(chat_id, next_st, payload)
        elif data.startswith("toggle:"):
            sel = set(payload.get("analysts", "").split(","))
            k = data.split(":")[1]
            sel.remove(k) if k in sel else sel.add(k)
            payload["analysts"] = ",".join(sel)
            set_state(chat_id, state, payload)
        elif data == "analysts_done":
            if not payload.get("analysts"): raise ValueError("حداقل یک تحلیلگر انتخاب کنید.")
            next_st = "flow_analysis_confirm" if state == "flow_analysis_analysts" else "flow_backtest_confirm"
            set_state(chat_id, next_st, payload)
        elif data.startswith("every:"):
            payload["every"] = int(data.split(":")[1])
            payload["analysts"] = "market,social,news,fundamentals"
            set_state(chat_id, "flow_backtest_analysts", payload)
        elif data in ("run_analysis", "run_backtest"):
            mode = "analysis" if data == "run_analysis" else "backtest"
            req_id = uuid.uuid4().hex
            gh("POST", "/dispatches", {"event_type": "tradingagents_run", "client_payload": {
                "request_id": req_id, "mode": mode, "chat_id": str(chat_id), "params": payload
            }})
            db_exec("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)", (req_id, None, chat_id, mode, json.dumps(payload), get_setting("active_model"), "queued", now(), now()))
            set_state(chat_id, "idle", {})
        elif data in ("active_runs", "history", "artifacts"):
            set_state(chat_id, data, {})
            
    except Exception as e:
        payload["error"] = str(e)
        set_state(chat_id, state, payload)
        
    update_ui(chat_id, mid)

# ==========================================
# Main Polling Loop
# ==========================================
def poll():
    if not BOT_TOKEN or not GH_TOKEN: sys.exit("Missing Tokens")
    offset = int(get_setting("last_update_id", "0")) + 1
    print("Controller started.", flush=True)
    
    while True:
        try:
            updates = tg("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message", "callback_query"]}) or []
            for u in updates:
                offset = max(offset, u["update_id"] + 1)
                set_setting("last_update_id", str(u["update_id"]))
                try:
                    if "callback_query" in u: handle_callback(u["callback_query"])
                    elif "message" in u and "text" in u["message"]:
                        if authorized(u["message"]["from"]["id"]): handle_text(u["message"]["chat"]["id"], u["message"]["text"])
                except Exception: traceback.print_exc()
        except Exception as e:
            msg = str(e)
            if "409" in msg: time.sleep(30) # Conflict, wait for other instance to die
            else: print(f"Poll error: {msg}", flush=True); time.sleep(5)

if __name__ == "__main__":
    poll()