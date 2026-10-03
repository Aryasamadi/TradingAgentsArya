# ruff: noqa: RUF001, RUF002, RUF003
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller - build v6.

UI rule (final):
- /start creates ONE new menu message and closes the previous menu
  (its buttons are removed so stale clicks are impossible).
- Every other button EDITS the single active menu message.
- No screen ever opens a second message; reports/logs are delivered
  as separate documents (that is content, not navigation).
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
from typing import Any

BUILD = "v6"

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
ANALYST_LABEL = {"market": "📊 Market", "social": "💬 Sentiment", "news": "📰 News", "fundamentals": "💰 Fundamentals"}
TERMINAL = ("success", "failure", "cancelled")

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ui (
        chat_id INTEGER PRIMARY KEY,
        message_id INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS models (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
        base_url TEXT NOT NULL, token_ciphertext BLOB,
        enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS runs (
        request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, chat_id INTEGER NOT NULL,
        mode TEXT NOT NULL, payload_json TEXT NOT NULL, model_id TEXT NOT NULL,
        status TEXT NOT NULL, conclusion TEXT NOT NULL DEFAULT '',
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    """
)
for stmt in (
    "ALTER TABLE runs ADD COLUMN workflow_run_id INTEGER",
    "ALTER TABLE runs ADD COLUMN conclusion TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0",
):
    with contextlib.suppress(sqlite3.OperationalError):
        DB.execute(stmt)
DB.commit()

DB_LOCK = threading.Lock()
CACHE: dict[str, Any] = {"runs": [], "artifacts": [], "ts": 0.0}
WIZARDS: dict[int, dict[str, str]] = {}
FLOWS: dict[int, dict[str, Any]] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def q(sql: str, args: tuple | list = ()):
    with DB_LOCK:
        return DB.execute(sql, args).fetchall()


def q1(sql: str, args: tuple | list = ()):
    with DB_LOCK:
        return DB.execute(sql, args).fetchone()


def db(sql: str, args: tuple | list = ()):
    with DB_LOCK:
        cur = DB.execute(sql, args)
        DB.commit()
        return cur


def get_setting(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return str(row[0]) if row else default


def set_setting(key: str, value: str) -> None:
    db("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def authorized(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in ALLOWED_IDS


def admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------- HTTP ----------------
class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq is not None:
            newreq.remove_header("Authorization")
        return newreq


_HTTP_OPENER = urllib.request.build_opener(_StripAuthRedirect())


def http_json(url: str, method: str = "GET", data: Any = None, headers: dict | None = None, timeout: int = 30):
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
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        with contextlib.suppress(Exception):
            body_bytes = exc.read()
        raise RuntimeError(f"HTTP {exc.code} — {body_bytes.decode('utf-8', 'replace')[:400]}") from exc


def gh(method: str, path: str, data: Any = None, timeout: int = 30):
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
    }
    status, obj = http_json(GH + path, method, data, headers, timeout=timeout)
    if status >= 300:
        raise RuntimeError(f"GitHub API error {status}")
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
    _, obj = http_json(f"{TG}/{method}", "POST", payload or {}, timeout=65)
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API {method} failed")
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
        f"{TG}/sendDocument", data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        resp.read()


def answer_callback(query_id: str, text: str = "", alert: bool = False) -> None:
    with contextlib.suppress(Exception):
        tg("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})


# ---------------- single-message UI core ----------------
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
        tg("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text[:4096],
            "parse_mode": "HTML", "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": inline(rows)},
        })
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


def close_menu(chat_id: int, message_id: int) -> None:
    """Deactivate an old menu: clear its buttons so stale clicks die."""
    if not message_id:
        return
    with contextlib.suppress(Exception):
        tg("editMessageText", {
            "chat_id": chat_id, "message_id": message_id,
            "text": "📴 این منو بسته شد؛ منوی فعال، پیام جدیدتر است.",
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": []},
        })


def show(chat_id: int, text: str, rows: list[list[tuple[str, str]]]) -> int:
    """Render onto the single active menu message; create one only if needed."""
    mid = ui_message(chat_id)
    if mid and edit_message(chat_id, mid, text, rows):
        remember_ui(chat_id, mid)
        return mid
    new_mid = send_message(chat_id, text, rows)
    remember_ui(chat_id, new_mid)
    return new_mid


# ---------------- models ----------------
PROVIDER_SECRET = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY", "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "qwen-cn": "DASHSCOPE_CN_API_KEY", "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY", "minimax": "MINIMAX_API_KEY", "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "mistral": "MISTRAL_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY", "nvidia": "NVIDIA_API_KEY", "perplexity": "PERPLEXITY_API_KEY",
    "together": "TOGETHER_API_KEY", "fireworks": "FIREWORKS_API_KEY", "cerebras": "CEREBRAS_API_KEY",
    "sambanova": "SAMBANOVA_API_KEY", "deepinfra": "DEEPINFRA_API_KEY", "cohere": "COHERE_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY", "bedrock": "AWS_BEARER_TOKEN_BEDROCK", "ollama": "",
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
        ("api.openai.com", "openai"), ("api.anthropic.com", "anthropic"),
        ("generativelanguage.googleapis.com", "google"), ("api.x.ai", "xai"),
        ("api.deepseek.com", "deepseek"), ("dashscope-intl.aliyuncs.com", "qwen"),
        ("dashscope.aliyuncs.com", "qwen-cn"), ("api.z.ai", "glm"),
        ("open.bigmodel.cn", "glm-cn"), ("api.minimax.io", "minimax"),
        ("api.minimaxi.com", "minimax-cn"), ("openrouter.ai", "openrouter"),
        ("api.mistral.ai", "mistral"), ("api.moonshot.ai", "kimi"),
        ("api.groq.com", "groq"), ("integrate.api.nvidia.com", "nvidia"),
        ("api.perplexity.ai", "perplexity"), ("api.together.xyz", "together"),
        ("api.fireworks.ai", "fireworks"), ("api.cerebras.ai", "cerebras"),
        ("api.sambanova.ai", "sambanova"), ("api.deepinfra.com", "deepinfra"),
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


def model_rows():
    return q("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE")


def active_model_id() -> str:
    return get_setting("active_model", "")


def set_active_model(model_id: str) -> None:
    set_setting("active_model", model_id)


def github_secret(name: str, value: str) -> None:
    from nacl import encoding, public
    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode("utf-8"))
    gh("PUT", f"/actions/secrets/{urllib.parse.quote(name, safe='')}",
       {"encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key["key_id"]})


def write_active_model_secrets(row) -> None:
    provider = row["provider"]
    token = decrypt_token(row["token_ciphertext"])
    if provider in PROVIDER_SECRET and PROVIDER_SECRET[provider] and token:
        github_secret(PROVIDER_SECRET[provider], token)
    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", row["name"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", row["base_url"])
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")


def add_model(name: str, provider: str, base_url: str, token: str) -> str:
    model_id = "m_" + uuid.uuid4().hex[:12]
    db("INSERT INTO models(id,name,provider,base_url,token_ciphertext,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
       (model_id, name, provider, base_url, encrypt_token(token), now()))
    row = q1("SELECT * FROM models WHERE id=?", (model_id,))
    write_active_model_secrets(row)
    set_active_model(model_id)
    return model_id


def post_model_test(model, timeout: int = 120) -> float:
    provider = model["provider"]
    base_url = model["base_url"].rstrip("/")
    token = decrypt_token(model["token_ciphertext"])
    name = model["name"]
    started = time.monotonic()
    try:
        if provider == "anthropic":
            endpoint = base_url if base_url.endswith("/messages") else base_url + "/v1/messages"
            status, _ = http_json(endpoint, "POST",
                                  {"model": name, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK only."}]},
                                  {"x-api-key": token, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                                  timeout=timeout)
        elif provider == "google":
            endpoint = base_url + "/models/" + urllib.parse.quote(name, safe="") + ":generateContent"
            endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode({"key": token})
            status, _ = http_json(endpoint, "POST",
                                  {"contents": [{"parts": [{"text": "Reply OK only."}]}], "generationConfig": {"maxOutputTokens": 8}},
                                  {"Content-Type": "application/json"}, timeout=timeout)
        else:
            endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            status, _ = http_json(endpoint, "POST",
                                  {"model": name, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8},
                                  headers, timeout=timeout)
        if status < 200 or status >= 300:
            raise RuntimeError(f"HTTP {status}")
    except Exception as exc:
        raise RuntimeError(f"{exc}\nProvider: {provider}\nURL: {base_url}\nModel: {name}") from exc
    return time.monotonic() - started


# ---------------- github runs ----------------
def fetch_active_runs():
    active = []
    for status in ("queued", "in_progress"):
        data = gh("GET", f"/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=50", timeout=15) or {}
        for run in data.get("workflow_runs", []):
            if run.get("event") == "repository_dispatch":
                active.append(run)
    return list({int(run["id"]): run for run in active if run.get("id")}.values())


def dispatch_run(chat_id: int, mode: str, params: dict) -> str:
    model_id = active_model_id()
    if not model_id:
        raise ValueError("اول از بخش مدل‌ها یک مدل فعال کن.")
    request_id = uuid.uuid4().hex
    gh("POST", "/dispatches",
       {"event_type": "tradingagents_run",
        "client_payload": {"request_id": request_id, "mode": mode, "chat_id": str(chat_id),
                           "model_id": model_id, "params": params}})
    db("INSERT INTO runs(request_id,workflow_run_id,chat_id,mode,payload_json,model_id,status,conclusion,notified,created_at,updated_at) "
       "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
       (request_id, None, chat_id, mode, json.dumps(params, ensure_ascii=False), model_id, "queued", "", now(), now()))
    return request_id


def sync_run_records() -> None:
    rows = q("SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 30")
    if not rows:
        return
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
        db("UPDATE runs SET workflow_run_id=?, status=?, conclusion=?, updated_at=? WHERE request_id=?",
           (run.get("id"), final, conclusion, now(), row["request_id"]))


def notify_finished() -> None:
    rows = q("SELECT * FROM runs WHERE notified=0 AND status IN " + str(TERMINAL) + " LIMIT 10")
    for row in rows:
        params = json.loads(row["payload_json"] or "{}")
        subject = params.get("ticker") or params.get("tickers") or "—"
        label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"
        emoji = "✅" if row["status"] == "success" else "❌"
        text = (f"{emoji} {label} <code>{esc(subject)}</code> تمام شد.\n"
                f"نتیجه: <b>{esc(row['status'])}</b>\n"
                "برای دریافت گزارش کامل دکمه زیر را بزن.")
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
            lambda: CACHE.update(artifacts=(gh("GET", "/actions/artifacts?per_page=15", timeout=15) or {}).get("artifacts", [])),
            notify_finished,
        ):
            with contextlib.suppress(Exception):
                task()
        CACHE["ts"] = time.monotonic()
        time.sleep(20)


def cancel_run(run_id: int) -> None:
    gh("POST", f"/actions/runs/{run_id}/cancel", timeout=15)


# ---------------- output viewer ----------------
def extract_summary(body: str) -> str:
    patterns = [
        r"(?i)(?:##?\s*?(?:Final|نتیجه|خلاصه|تصمیم|Decision|Conclusion|Summary)[^\n]*\n)([\s\S]{50,1500}?)(?=\n##|\Z)",
        r"(?i)(?:Recommendation|پیشنهاد|توصیه)[^\n]*\n([\s\S]{50,1500}?)(?=\n##|\Z)",
    ]
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            snippet = match.group(1).strip()
            snippet = re.sub(r"```[\s\S]*?```", "", snippet).strip()
            if snippet:
                return snippet[:1200]
    clean = re.sub(r"```[\s\S]*?```", "", body).strip()
    return clean[:1000] if clean else "(خلاصه‌ای یافت نشد)"


def send_output(chat_id: int, request_id: str) -> None:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row:
        send_message(chat_id, "❌ این اجرا پیدا نشد.")
        return
    if not row["workflow_run_id"]:
        with contextlib.suppress(Exception):
            sync_run_records()
        row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    run_id = row["workflow_run_id"]
    if not run_id:
        send_message(chat_id, "⏳ هنوز Run ID ثبت نشده.")
        return
    try:
        data = gh("GET", f"/actions/runs/{run_id}/artifacts") or {}
        arts = [a for a in data.get("artifacts", []) if not a.get("expired")]
        pick = None
        for prefix in ("tradingagents-results-", "tradingagents-run-"):
            for art in arts:
                if str(art.get("name", "")).startswith(prefix):
                    pick = art
                    break
            if pick:
                break
        if not pick:
            send_message(chat_id, "📭 هنوز Artifact خروجی برای این اجرا ساخته نشده است.")
            return
        raw = gh_bytes(f"/actions/artifacts/{pick['id']}/zip")
    except Exception as exc:
        send_message(chat_id, f"❌ دریافت خروجی ناموفق: {esc(exc)}")
        return
    zf = zipfile.ZipFile(io.BytesIO(raw))
    scored = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        low = name.lower()
        if low.endswith((".py", ".db", ".sqlite", ".zip", ".sh", ".yml", ".yaml")):
            continue
        if "bot.py" in low or "tradingagents.yml" in low or "__pycache__" in low:
            continue
        if not low.endswith((".md", ".txt", ".log", ".json")):
            continue
        try:
            body = zf.read(name).decode("utf-8", "replace")
        except Exception:
            continue
        score = 0
        if "full_report" in low or "complete_report" in low:
            score += 10
        if "report" in low:
            score += 3
        if low.endswith(".md"):
            score += 2
        scored.append((score, name, body))
    if not scored:
        send_message(chat_id, "📭 گزارش متنی داخل Artifact پیدا نشد.")
        return
    scored.sort(key=lambda item: -item[0])
    best_name = scored[0][1].replace("/", "_")
    best_body = scored[0][2]
    label = "تحلیل" if row["mode"] == "analysis" else "بک‌تست"
    tg_document(chat_id, best_name, best_body.encode("utf-8"), f"📄 گزارش کامل {label} — {best_name}")
    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"
    send_message(
        chat_id,
        f"<b>📊 خلاصهٔ {label} {esc(subject)}</b>\n\n{esc(extract_summary(best_body))}\n\n📎 فایل کامل بالا ارسال شد.",
    )


def download_log(chat_id: int, artifact_id: str) -> None:
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
        send_message(chat_id, "📭 فایل لاگ داخل Artifact پیدا نشد.")
        return
    body = zf.read(log_name).decode("utf-8", "replace")
    tg_document(chat_id, "agent.log", body.encode("utf-8"), "📋 لاگ خام اجرا")


# ---------------- validation ----------------
def valid_date(value: str) -> str:
    value = value.strip().lower()
    if value in ("today", "امروز"):
        return dt.date.today().isoformat()
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("❌ فرمت اشتباه است.\n✅ فرمت صحیح: YYYY-MM-DD\nمثال: 2026-10-03\nیا دکمهٔ «امروز» را بزن.") from exc
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
                raise ValueError(f"❌ Ticker نامعتبر: {part}\n✅ مثال: NVDA, AAPL, BTC-USD")
            if part not in values:
                values.append(part)
        return ",".join(values)
    if not raw or any(ch.isspace() for ch in raw):
        raise ValueError("❌ Ticker خالی است.\n✅ مثال: NVDA یا BTC-USD")
    if not all(ch.isalnum() or ch in "._-^=" for ch in raw) or len(raw) > 32:
        raise ValueError("فرمت Ticker نامعتبر است.")
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


# ---------------- screens (all edit the single active menu) ----------------
def home_screen(chat_id: int) -> int:
    aid = active_model_id()
    mrow = q1("SELECT name FROM models WHERE id=?", (aid,)) if aid else None
    model_line = f"مدل فعال: <b>{esc(mrow['name'])}</b>" if mrow else "مدل فعال: <b>تنظیم نشده</b>"
    busy = q1("SELECT COUNT(*) c FROM runs WHERE status NOT IN " + str(TERMINAL))["c"]
    live = f"\n🟡 اجرای فعال: <b>{busy}</b>" if busy else ""
    return show(chat_id,
                f"<b>🤖 TradingAgentsArya</b>\n{model_line}{live}\nیک گزینه را انتخاب کن.\n<code>build {BUILD}</code>",
                [
                    [("🚀 تحلیل جدید", "flow_analysis_start"), ("📈 بک‌تست", "flow_backtest_start")],
                    [("🤖 مدل‌ها", "models"), ("📊 اجراهای جاری", "active_runs")],
                    [("📄 خروجی‌ها", "outputs"), ("📋 لاگ‌ها", "logs")],
                ])


def models_screen(chat_id: int) -> int:
    rows = model_rows()
    aid = active_model_id()
    text = "<b>🤖 مدل‌ها</b>\n"
    buttons: list[list[tuple[str, str]]] = []
    if not rows:
        text += "هنوز مدلی ثبت نشده."
    for row in rows:
        mark = "✅ " if row["id"] == aid else ""
        text += f"\n{mark}<b>{esc(row['name'])}</b> <code>{esc(row['provider'])}</code>"
        buttons.append([(f"⚡ فعال‌سازی {row['name']}", f"activate:{row['id']}")])
    buttons += [
        [("➕ افزودن مدل", "model_add"), ("🧪 تست اتصال", "model_test_menu")],
        [("🗑 حذف مدل", "model_delete_menu")],
        [("🏠 خانه", "home")],
    ]
    return show(chat_id, text, buttons)


def wizard_url_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>➕ افزودن مدل (1 از 3)</b>\nBase URL را دقیقاً همان‌طور که هست بفرست.\n\n"
                "<b>مثال‌ها:</b>\n• <code>https://vyceai.com/v1</code>\n• <code>https://integrate.api.nvidia.com/v1</code>\n"
                "• <code>https://openrouter.ai/api/v1</code>\n\n❌ نامعتبر: بدون http:// یا https://",
                [[("❌ لغو", "wizard_cancel")]])


def wizard_token_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>➕ افزودن مدل (2 از 3)</b>\nAPI Token را بفرست.\n\n<b>مثال:</b> <code>sk-...</code>\n⚠️ حداقل ۳ کاراکتر.",
                [[("❌ لغو", "wizard_cancel")]])


def wizard_model_screen(chat_id: int) -> int:
    provider = WIZARDS.get(chat_id, {}).get("provider", "")
    hint = ""
    if provider == "nvidia":
        hint = "\nمثال: <code>meta/llama-3.1-405b-instruct</code>"
    elif provider == "openrouter":
        hint = "\nمثال: <code>openai/gpt-4o</code>"
    return show(chat_id,
                f"<b>➕ افزودن مدل (3 از 3)</b>\nModel ID دقیق را بفرست.\n"
                f"<b>Provider:</b> <code>{esc(provider)}</code>{hint}\n⚠️ بلافاصله ذخیره و فعال می‌شود.",
                [[("❌ لغو", "wizard_cancel")]])


def model_test_menu(chat_id: int) -> int:
    buttons = [[(f"🧪 {r['name']}", f"test_model:{r['id']}")] for r in model_rows()]
    buttons.append([("◀️ مدل‌ها", "models")])
    return show(chat_id,
                "<b>🧪 تست اتصال</b>\nیک درخواست بسیار کوچک واقعی به API؛ TradingAgents اجرا نمی‌شود.\nTimeout: ۱۲۰ ثانیه.",
                buttons)


def model_delete_menu(chat_id: int) -> int:
    buttons = [[(f"🗑 {r['name']}", f"delete_model:{r['id']}")] for r in model_rows()]
    buttons.append([("◀️ مدل‌ها", "models")])
    return show(chat_id, "<b>🗑 حذف مدل</b>\nمدل را انتخاب کن.", buttons)


def analysis_ticker_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>🔎 تحلیل جدید (مرحله 1 از 3)</b>\nTicker را بفرست.\n\n<b>مثال:</b> <code>NVDA</code> یا <code>BTC-USD</code>",
                [[("❌ لغو", "flow_cancel")]])


def analysis_date_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>🔎 تحلیل جدید (مرحله 2 از 3)</b>\nتاریخ تحلیل را بفرست.\n\n<b>فرمت:</b> YYYY-MM-DD\n<b>مثال:</b> <code>2026-10-03</code>\nیا دکمهٔ «امروز».",
                [[("📅 امروز", "analysis_today"), ("❌ لغو", "flow_cancel")]])


def backtest_tickers_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>📈 بک‌تست (مرحله 1 از 5)</b>\nیک یا چند Ticker بفرست.\n\n<b>مثال:</b> <code>NVDA,AAPL</code>",
                [[("❌ لغو", "flow_cancel")]])


def backtest_start_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>📈 بک‌تست (مرحله 2 از 5)</b>\nتاریخ شروع را بفرست.\n\n<b>فرمت:</b> YYYY-MM-DD\n<b>مثال:</b> <code>2026-01-01</code>",
                [[("❌ لغو", "flow_cancel")]])


def backtest_end_screen(chat_id: int) -> int:
    return show(chat_id,
                "<b>📈 بک‌تست (مرحله 3 از 5)</b>\nتاریخ پایان را بفرست.\n\n<b>فرمت:</b> YYYY-MM-DD\n<b>مثال:</b> <code>2026-10-01</code>\n⚠️ باید بعد از شروع باشد.",
                [[("📅 امروز", "backtest_today"), ("❌ لغو", "flow_cancel")]])


def backtest_every_screen(chat_id: int) -> int:
    return show(chat_id, "<b>📈 بک‌تست (مرحله 4 از 5)</b>\nفاصله زمانی را انتخاب کن.",
                [
                    [("📅 1 روز", "every:1"), ("📅 7 روز", "every:7")],
                    [("📅 14 روز", "every:14"), ("📅 30 روز", "every:30")],
                    [("❌ لغو", "flow_cancel")],
                ])


def analyst_picker(chat_id: int, backtest: bool) -> int:
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
    rows += [[("✅ تایید و ادامه", "analysts_done")], [("❌ لغو", "flow_cancel")]]
    return show(chat_id, "<b>🎛 تحلیلگران (مرحله آخر)</b>\nانتخاب‌شده‌ها با ✅ مشخص‌اند.", rows)


def analysis_confirm_screen(chat_id: int) -> int:
    data = FLOWS.get(chat_id, {})
    chosen = ", ".join(ANALYST_LABEL[x] for x in data.get("analysts", "").split(",") if x in ANALYST_LABEL) or "هیچ‌کدام"
    return show(chat_id,
                f"<b>✅ آماده اجرا</b>\nTicker: <code>{esc(data.get('ticker'))}</code>\n"
                f"تاریخ: <code>{esc(data.get('date'))}</code>\nتحلیلگران: <b>{esc(chosen)}</b>",
                [
                    [("🚀 شروع تحلیل", "analysis_run")],
                    [("🎛 تحلیلگران", "analysis_analysts"), ("📅 تغییر تاریخ", "analysis_change_date")],
                    [("❌ لغو", "flow_cancel")],
                ])


def backtest_confirm_screen(chat_id: int) -> int:
    data = FLOWS.get(chat_id, {})
    chosen = ", ".join(ANALYST_LABEL[x] for x in data.get("analysts", "").split(",") if x in ANALYST_LABEL) or "هیچ‌کدام"
    return show(chat_id,
                f"<b>✅ آماده بک‌تست</b>\nTickerها: <code>{esc(data.get('tickers'))}</code>\n"
                f"از <code>{esc(data.get('start'))}</code> تا <code>{esc(data.get('end'))}</code>\n"
                f"فاصله: <b>هر {esc(data.get('every', 7))} روز</b>\nتحلیلگران: <b>{esc(chosen)}</b>",
                [
                    [("🚀 شروع بک‌تست", "backtest_run")],
                    [("🎛 تحلیلگران", "backtest_analysts"), ("📅 فاصله", "backtest_every")],
                    [("❌ لغو", "flow_cancel")],
                ])


def active_runs_screen(chat_id: int) -> int:
    runs = list(CACHE.get("runs", []))
    lines = ["<b>📊 اجراهای جاری</b>"]
    buttons = []
    if runs:
        lines.append("")
        for run in sorted(runs, key=lambda x: str(x.get("created_at", "")), reverse=True):
            lines.append(f"🟡 <b>{esc(run.get('display_title') or 'Engine')}</b>")
            lines.append(f"⏱ شروع: {esc(format_run_time(run.get('created_at')))}")
            lines.append(f"Run: <code>{esc(run.get('id'))}</code>")
            buttons.append([("🛑 لغو این اجرا", f"cancelw:{run.get('id')}")])
            lines.append("")
    else:
        local = q("SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 5")
        if local:
            lines.append("")
            for row in local:
                lines.append(f"🟡 <code>{esc(row['request_id'][:8])}</code> | <b>{esc(row['status'])}</b>")
                lines.append(f"⏱ شروع: {esc(format_run_time(row['created_at']))}")
                if row["workflow_run_id"]:
                    buttons.append([("🛑 لغو این اجرا", f"cancelw:{row['workflow_run_id']}")])
                lines.append("")
        else:
            lines.append("✅ هیچ پردازش فعالی وجود ندارد.")
    buttons.append([("🔄 تازه‌سازی", "active_runs"), ("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons)


def outputs_screen(chat_id: int) -> int:
    with contextlib.suppress(Exception):
        sync_run_records()
    rows = q("SELECT * FROM runs ORDER BY created_at DESC LIMIT 12")
    if not rows:
        return show(chat_id, "<b>📄 خروجی‌ها</b>\nهنوز اجرایی ثبت نشده.", [[("🏠 خانه", "home")]])
    lines = ["<b>📄 خروجی‌ها</b>", ""]
    buttons = []
    for row in rows:
        params = json.loads(row["payload_json"] or "{}")
        subject = params.get("ticker") or params.get("tickers") or "—"
        try:
            created_str = dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_str = row["created_at"][:16]
        lines.append(f"• <code>{esc(row['request_id'][:8])}</code> {esc(subject)} | {esc(created_str)} | <b>{esc(row['status'])}</b>")
        buttons.append([("📄 گزارش", f"view_output:{row['request_id']}"), ("🗑 حذف", f"ask_delete:{row['request_id']}")])
    buttons.append([("🧹 پاک‌سازی کلی", "bulk_delete_menu"), ("🔄 تازه‌سازی", "outputs")])
    buttons.append([("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons)


def ask_delete_screen(chat_id: int, request_id: str) -> int:
    row = q1("SELECT * FROM runs WHERE request_id=?", (request_id,))
    if not row:
        return show(chat_id, "❌ این اجرا پیدا نشد.", [[("◀️ خروجی‌ها", "outputs")]])
    params = json.loads(row["payload_json"] or "{}")
    subject = params.get("ticker") or params.get("tickers") or "—"
    return show(chat_id,
                f"<b>⚠️ تأیید حذف</b>\n\nشناسه: <code>{esc(request_id[:10])}</code>\nموضوع: <b>{esc(subject)}</b>\n"
                f"وضعیت: <b>{esc(row['status'])}</b>\n\n⚠️ این عمل غیرقابل بازگشت است.",
                [
                    [("✅ بله، حذف شود", f"confirm_delete:{request_id}")],
                    [("❌ انصراف", "outputs")],
                ])


def bulk_delete_menu(chat_id: int) -> int:
    return show(chat_id,
                "<b>🧹 پاک‌سازی تاریخچه</b>\nچه بازه‌ای پاک شود؟\n⚠️ فقط رکوردهای محلی؛ Artifacts گیت‌هاب دست‌نخورده می‌مانند.",
                [
                    [("🗑 ۷ روز گذشته", "bulk_scope:7"), ("🗑 ۳۰ روز گذشته", "bulk_scope:30")],
                    [("🗑 همه", "bulk_scope:all")],
                    [("❌ انصراف", "outputs")],
                ])


def bulk_confirm_screen(chat_id: int, scope: str) -> int:
    label = {"7": "۷ روز گذشته", "30": "۳۰ روز گذشته", "all": "همهٔ رکوردها"}.get(scope, scope)
    return show(chat_id,
                f"<b>⚠️ تأیید پاک‌سازی</b>\nواقعاً <b>{esc(label)}</b> پاک شود؟",
                [
                    [("✅ بله، پاک شود", f"bulk_do:{scope}")],
                    [("❌ انصراف", "outputs")],
                ])


def logs_screen(chat_id: int) -> int:
    items = [a for a in CACHE.get("artifacts", []) if not a.get("expired")]
    run_logs = [i for i in items if str(i.get("name", "")).startswith("tradingagents-run-")][:15]
    if not run_logs:
        return show(chat_id, "<b>📋 لاگ‌ها</b>\nهنوز لاگ خامی تولید نشده.",
                    [[("🔄 تازه‌سازی", "logs"), ("🏠 خانه", "home")]])
    lines = ["<b>📋 لاگ‌ها</b>", ""]
    buttons = []
    for idx, item in enumerate(run_logs, 1):
        try:
            created_str = dt.datetime.fromisoformat(str(item.get("created_at")).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created_str = str(item.get("created_at"))[:16]
        lines.append(f"• <b>#{idx}</b> — {esc(created_str)}")
        buttons.append([(f"📋 لاگ #{idx}", f"download_log:{item['id']}")])
    buttons.append([("🔄 تازه‌سازی", "logs"), ("🏠 خانه", "home")])
    return show(chat_id, "\n".join(lines), buttons)


# ---------------- text handling ----------------
def handle_text(chat_id: int, text: str) -> None:
    text = text.strip()
    if text.startswith("/start"):
        WIZARDS.pop(chat_id, None)
        FLOWS.pop(chat_id, None)
        old = ui_message(chat_id)
        close_menu(chat_id, old)
        remember_ui(chat_id, 0)
        home_screen(chat_id)
        return

    if chat_id in WIZARDS:
        item = WIZARDS[chat_id]
        stage = item.get("stage")
        try:
            if stage == "url":
                item["url"] = normalize_base_url(text)
                item["provider"] = infer_provider(item["url"])
                item["stage"] = "token"
                wizard_token_screen(chat_id)
            elif stage == "token":
                if len(text) < 3:
                    raise ValueError("Token خیلی کوتاه است (حداقل ۳ کاراکتر).")
                item["token"] = text
                item["stage"] = "model"
                wizard_model_screen(chat_id)
            elif stage == "model":
                if not text:
                    raise ValueError("Model ID نمی‌تواند خالی باشد.")
                model_id = add_model(text, item["provider"], item["url"], item.get("token", ""))
                WIZARDS.pop(chat_id, None)
                show(chat_id,
                     f"<b>✅ مدل ذخیره و فعال شد</b>\nModel: <code>{esc(text)}</code>\n"
                     f"Provider: <code>{esc(item['provider'])}</code>",
                     [[("🧪 تست اتصال", f"test_model:{model_id}"), ("🏠 خانه", "home")]])
        except ValueError as exc:
            show(chat_id, f"<b>⚠️ {esc(exc)}</b>\n\nدوباره تلاش کن:", [[("❌ لغو", "wizard_cancel")]])
        return

    if chat_id in FLOWS:
        flow = FLOWS[chat_id]
        kind = flow.get("kind")
        stage = flow.get("stage")
        try:
            if kind == "analysis" and stage == "ticker":
                flow["ticker"] = valid_ticker(text)
                flow["stage"] = "date"
                analysis_date_screen(chat_id)
            elif kind == "analysis" and stage == "date":
                flow["date"] = valid_date(text)
                flow["stage"] = "analysts"
                analyst_picker(chat_id, backtest=False)
            elif kind == "backtest" and stage == "tickers":
                flow["tickers"] = valid_ticker(text, allow_many=True)
                flow["stage"] = "start"
                backtest_start_screen(chat_id)
            elif kind == "backtest" and stage == "start":
                flow["start"] = valid_date(text)
                flow["stage"] = "end"
                backtest_end_screen(chat_id)
            elif kind == "backtest" and stage == "end":
                end = valid_date(text)
                if end < flow.get("start", end):
                    raise ValueError("❌ تاریخ پایان باید بعد از شروع باشد.")
                flow["end"] = end
                flow["stage"] = "every"
                backtest_every_screen(chat_id)
        except ValueError as exc:
            show(chat_id, f"<b>⚠️ ورودی درست نیست</b>\n{esc(exc)}\n\nهمین مرحله را دوباره وارد کن:",
                 [[("❌ لغو", "flow_cancel")]])


# ---------------- callback handling ----------------
def callback(query: dict[str, Any]) -> None:
    user_id = int(query.get("from", {}).get("id", 0))
    if not authorized(user_id):
        answer_callback(query["id"], "دسترسی مجاز نیست.", True)
        return
    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    data = query.get("data", "")
    answer_callback(query["id"])

    try:
        if data == "home":
            WIZARDS.pop(chat_id, None)
            FLOWS.pop(chat_id, None)
            home_screen(chat_id)
        elif data == "models":
            models_screen(chat_id)
        elif data == "model_add":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل اضافه کند.")
            WIZARDS[chat_id] = {"stage": "url"}
            wizard_url_screen(chat_id)
        elif data == "wizard_cancel":
            WIZARDS.pop(chat_id, None)
            models_screen(chat_id)
        elif data == "model_test_menu":
            model_test_menu(chat_id)
        elif data.startswith("test_model:"):
            model_id = data.split(":", 1)[1]
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (model_id,))
            if not row:
                raise ValueError("مدل پیدا نشد.")
            try:
                elapsed = post_model_test(row)
                show(chat_id,
                     f"<b>✅ تست موفق</b>\nمدل: <code>{esc(row['name'])}</code>\n"
                     f"Provider: <code>{esc(row['provider'])}</code>\nزمان پاسخ: <b>{elapsed:.1f}s</b>",
                     [[("🧪 تست دوباره", f"test_model:{model_id}"), ("◀️ مدل‌ها", "models")]])
            except Exception as exc:
                show(chat_id,
                     f"<b>❌ تست ناموفق</b>\nمدل: <code>{esc(row['name'])}</code>\n\n{esc(str(exc))}",
                     [[("🧪 تست دوباره", f"test_model:{model_id}"), ("◀️ مدل‌ها", "models")]])
        elif data.startswith("activate:"):
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (data.split(":", 1)[1],))
            if not row:
                raise ValueError("مدل پیدا نشد.")
            write_active_model_secrets(row)
            set_active_model(row["id"])
            show(chat_id, f"<b>⚡ مدل فعال شد</b>\n<code>{esc(row['name'])}</code>", [[("◀️ مدل‌ها", "models")]])
        elif data == "model_delete_menu":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            model_delete_menu(chat_id)
        elif data.startswith("delete_model:"):
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            model_id = data.split(":", 1)[1]
            db("UPDATE models SET enabled=0 WHERE id=?", (model_id,))
            if active_model_id() == model_id:
                set_active_model("")
            show(chat_id, "<b>🗑 مدل حذف شد.</b>", [[("◀️ مدل‌ها", "models")]])
        elif data == "active_runs":
            active_runs_screen(chat_id)
        elif data.startswith("cancelw:"):
            run_id = int(data.split(":", 1)[1])
            try:
                cancel_run(run_id)
                show(chat_id, f"<b>🛑 درخواست لغو ارسال شد</b>\nRun: {run_id}",
                     [[("◀️ اجراهای جاری", "active_runs")]])
            except Exception as exc:
                show(chat_id,
                     f"<b>❌ لغو ناموفق</b>\n{esc(exc)}\n\n⚠️ مطمئن شو BOT_GITHUB_TOKEN دسترسی workflow دارد.",
                     [[("◀️ اجراهای جاری", "active_runs")]])
        elif data == "outputs":
            outputs_screen(chat_id)
        elif data.startswith("view_output:"):
            answer_callback(query["id"], "📄 در حال آماده‌سازی خروجی...")
            threading.Thread(target=send_output, args=(chat_id, data.split(":", 1)[1]), daemon=True).start()
        elif data.startswith("ask_delete:"):
            ask_delete_screen(chat_id, data.split(":", 1)[1])
        elif data.startswith("confirm_delete:"):
            db("DELETE FROM runs WHERE request_id=?", (data.split(":", 1)[1],))
            show(chat_id, "<b>✅ خروجی حذف شد.</b>", [[("◀️ خروجی‌ها", "outputs")]])
        elif data == "bulk_delete_menu":
            bulk_delete_menu(chat_id)
        elif data.startswith("bulk_scope:"):
            bulk_confirm_screen(chat_id, data.split(":", 1)[1])
        elif data.startswith("bulk_do:"):
            scope = data.split(":", 1)[1]
            if scope == "all":
                cur = db("DELETE FROM runs")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(scope))).isoformat()
                cur = db("DELETE FROM runs WHERE created_at < ?", (cutoff,))
            show(chat_id, f"<b>✅ پاک‌سازی انجام شد</b>\n{cur.rowcount} رکورد حذف شد.",
                 [[("◀️ خروجی‌ها", "outputs")]])
        elif data == "logs":
            logs_screen(chat_id)
        elif data.startswith("download_log:"):
            answer_callback(query["id"], "📋 در حال دانلود لاگ...")
            threading.Thread(target=download_log, args=(chat_id, data.split(":", 1)[1]), daemon=True).start()
        elif data == "flow_analysis_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            FLOWS[chat_id] = {"kind": "analysis", "stage": "ticker", "analysts": "market,social,news,fundamentals"}
            analysis_ticker_screen(chat_id)
        elif data == "flow_backtest_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            FLOWS[chat_id] = {"kind": "backtest", "stage": "tickers", "every": 7, "analysts": "market,social,news,fundamentals"}
            backtest_tickers_screen(chat_id)
        elif data == "flow_cancel":
            FLOWS.pop(chat_id, None)
            home_screen(chat_id)
        elif data == "analysis_today":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            flow["date"] = dt.date.today().isoformat()
            flow["stage"] = "analysts"
            analyst_picker(chat_id, backtest=False)
        elif data == "analysis_change_date":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            flow["stage"] = "date"
            analysis_date_screen(chat_id)
        elif data == "analysis_analysts":
            analyst_picker(chat_id, backtest=False)
        elif data == "backtest_today":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            end = dt.date.today().isoformat()
            if end < flow.get("start", end):
                raise ValueError("امروز قبل از تاریخ شروع است.")
            flow["end"] = end
            flow["stage"] = "every"
            backtest_every_screen(chat_id)
        elif data == "backtest_every":
            backtest_every_screen(chat_id)
        elif data == "backtest_analysts":
            analyst_picker(chat_id, backtest=True)
        elif data.startswith("every:"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            flow["every"] = int(data.split(":", 1)[1])
            flow["stage"] = "analysts"
            analyst_picker(chat_id, backtest=True)
        elif data.startswith("toggle_analyst:"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            key = data.split(":", 1)[1]
            selected = {x for x in flow.get("analysts", "").split(",") if x}
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
            flow["analysts"] = ",".join(x for x in ANALYST_ORDER if x in selected)
            analyst_picker(chat_id, backtest=flow.get("kind") == "backtest")
        elif data == "analysts_done":
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله منقضی شده است.")
            if not flow.get("analysts"):
                raise ValueError("حداقل یک تحلیلگر انتخاب کن.")
            if flow.get("kind") == "analysis":
                analysis_confirm_screen(chat_id)
            else:
                backtest_confirm_screen(chat_id)
        elif data in ("analysis_run", "backtest_run"):
            flow = FLOWS.get(chat_id)
            if not flow:
                raise ValueError("این مرحله دیگر فعال نیست.")
            if flow.get("kind") == "analysis":
                params = {"ticker": flow["ticker"], "date": flow["date"], "analysts": flow.get("analysts", "")}
                mode = "analysis"
            else:
                params = {"tickers": flow["tickers"], "start": flow["start"], "end": flow["end"],
                          "every": int(flow.get("every", 7)), "analysts": flow.get("analysts", "")}
                mode = "backtest"
            request_id = dispatch_run(chat_id, mode, params)
            FLOWS.pop(chat_id, None)
            label = "تحلیل" if mode == "analysis" else "بک‌تست"
            show(chat_id,
                 f"<b>🚀 {label} در صف اجرا قرار گرفت</b>\nشناسه: <code>{esc(request_id[:10])}</code>",
                 [[("📊 اجراهای جاری", "active_runs"), ("🏠 خانه", "home")]])
    except Exception as exc:
        show(chat_id, f"<b>❌ خطا</b>\n{esc(exc)}", [[("🏠 خانه", "home")]])


# ---------------- polling ----------------
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
                            handle_text(int(update["message"]["chat"]["id"]), update["message"]["text"])
                except Exception:
                    sys.stderr.write(traceback.format_exc())
        except Exception as exc:
            message = str(exc)
            if "409" in message or "Conflict" in message:
                if not conflict_started:
                    conflict_started = time.monotonic()
                    sys.stdout.write("Another controller is active; waiting...\n")
                    sys.stdout.flush()
                if time.monotonic() - conflict_started > 120:
                    sys.stdout.write("Second controller still active; stopping.\n")
                    sys.stdout.flush()
                    return
                time.sleep(10)
            else:
                sys.stdout.write(f"Polling error: {message}\n")
                sys.stdout.flush()
                time.sleep(5)


if __name__ == "__main__":
    poll()