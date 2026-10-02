# ruff: noqa: RUF001, RUF002, RUF003
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller - build v4."""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import html
import io
import json
import os
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

BUILD = "v4"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GH_TOKEN = os.getenv("BOT_GITHUB_TOKEN", "").strip()
STATE_KEY = os.getenv("BOT_STATE_KEY", "").strip()
OWNER = os.getenv("GITHUB_OWNER", "Aryasamadi").strip()
REPO = os.getenv("GITHUB_REPO", "TradingAgentsArya").strip()
STATE_PATH = os.getenv("BOT_STATE_PATH", "bot_state.db").strip() or "bot_state.db"
ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ALLOWED_IDS = {int(x) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}

TG = "https://api.telegram.org/bot" + BOT_TOKEN
GH = "https://api.github.com/repos/" + OWNER + "/" + REPO

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_LABEL = {"market": "📊 Market", "social": "💬 Sentiment", "news": "📰 News", "fundamentals": "💰 Fundamentals"}
TERMINAL = ("success", "failure", "cancelled")

DB = sqlite3.connect(STATE_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA foreign_keys=ON")
DB.executescript(
    """
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
    CREATE TABLE IF NOT EXISTS chat_state (
        chat_id INTEGER PRIMARY KEY, state TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        ui_message_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
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
UI_HASH: dict[int, str] = {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def q(sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
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


# ---------------- http ----------------
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
        raise RuntimeError("HTTP " + str(exc.code)) from exc


def http_bytes(url: str, headers: dict | None = None, timeout: int = 120) -> bytes:
    req_headers = {"Accept": "application/zip", "User-Agent": "TradingAgentsArya-Bot"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP " + str(exc.code)) from exc


def tg(method: str, payload: dict | None = None):
    _, obj = http_json(TG + "/" + method, "POST", payload or {}, timeout=65)
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError("Telegram API " + method + " failed")
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
        TG + "/sendDocument", data=buf.getvalue(),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def gh(method: str, path: str, data: Any = None, timeout: int = 30):
    if not GH_TOKEN:
        raise RuntimeError("BOT_GITHUB_TOKEN تنظیم نشده است.")
    headers = {
        "Authorization": "Bearer " + GH_TOKEN,
        "X-GitHub-Api-Version": "2026-03-10",
        "Accept": "application/vnd.github+json",
    }
    status, obj = http_json(GH + path, method, data, headers, timeout=timeout)
    if status >= 300:
        raise RuntimeError("GitHub API error " + str(status))
    return obj


def gh_bytes(path: str) -> bytes:
    return http_bytes(GH + path, {"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/zip"})


def answer_callback(query_id: str, text: str = "", alert: bool = False) -> None:
    with contextlib.suppress(Exception):
        tg("answerCallbackQuery", {"callback_query_id": query_id, "text": text[:200], "show_alert": alert})


# ---------------- ui core ----------------
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
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows is not None:
        payload["reply_markup"] = {"inline_keyboard": inline(rows)}
    return int(tg("sendMessage", payload)["message_id"])


def edit_message(chat_id: int, message_id: int, text: str, rows: list[list[tuple[str, str]]]) -> bool:
    try:
        tg(
            "editMessageText",
            {
                "chat_id": chat_id, "message_id": message_id, "text": text[:4096],
                "parse_mode": "HTML", "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": inline(rows)},
            },
        )
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False


def get_state(chat_id: int) -> tuple[str, dict, int]:
    row = q1("SELECT state, payload, ui_message_id FROM chat_state WHERE chat_id=?", (chat_id,))
    if not row:
        return "idle", {}, 0
    return row["state"], json.loads(row["payload"]), int(row["ui_message_id"])


def set_state(chat_id: int, state: str, payload: dict, ui_message_id: int | None = None) -> None:
    mid = ui_message_id if ui_message_id is not None else get_state(chat_id)[2]
    db(
        "INSERT INTO chat_state(chat_id,state,payload,ui_message_id,updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, payload=excluded.payload, "
        "ui_message_id=excluded.ui_message_id, updated_at=excluded.updated_at",
        (chat_id, state, json.dumps(payload, ensure_ascii=False), mid, now()),
    )


def update_ui(chat_id: int, cb_mid: int | None = None) -> None:
    state, payload, saved = get_state(chat_id)
    target = cb_mid or saved
    text, rows = build_ui(state, payload)
    digest = hashlib.sha256((text + json.dumps(rows, ensure_ascii=False)).encode("utf-8")).hexdigest()
    if target and target == saved and UI_HASH.get(chat_id) == digest:
        set_state(chat_id, state, payload, saved)
        return
    if target and edit_message(chat_id, target, text, rows):
        UI_HASH[chat_id] = digest
        set_state(chat_id, state, payload, target)
        return
    new_mid = send_message(chat_id, text, rows)
    UI_HASH[chat_id] = digest
    set_state(chat_id, state, payload, new_mid)


# ---------------- models ----------------
PROVIDER_SECRET = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GOOGLE_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY", "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY", "qwen-cn": "DASHSCOPE_CN_API_KEY", "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY", "minimax": "MINIMAX_API_KEY", "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY", "mistral": "MISTRAL_API_KEY", "kimi": "MOONSHOT_API_KEY",
    "groq": "GROQ_API_KEY", "nvidia": "NVIDIA_API_KEY",
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY", "bedrock": "AWS_BEARER_TOKEN_BEDROCK", "ollama": "",
}


def crypt_key() -> bytes:
    seed = STATE_KEY or ("legacy:" + OWNER + ":" + REPO + ":" + GH_TOKEN)
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
        raise RuntimeError("کلید رمزگشایی تغییر کرده؛ مدل را دوباره اضافه کن.") from exc


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
    ]
    for needle, provider in mapping:
        if needle == host or host.endswith("." + needle):
            return provider
    if "openai.azure.com" in host or host.endswith("cognitiveservices.azure.com"):
        return "azure"
    if "bedrock-runtime" in host and host.endswith("amazonaws.com"):
        return "bedrock"
    if host in ("localhost", "127.0.0.1") or host.endswith(":11434"):
        return "ollama"
    return "openai_compatible"


def model_rows() -> list[sqlite3.Row]:
    return q("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE")


def active_model_id() -> str:
    return get_setting("active_model")


def github_secret(name: str, value: str) -> None:
    from nacl import encoding, public
    key = gh("GET", "/actions/secrets/public-key")
    public_key = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(public_key).encrypt(value.encode("utf-8"))
    gh("PUT", "/actions/secrets/" + urllib.parse.quote(name, safe=""),
       {"encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key["key_id"]})


def write_active_model_secrets(row: sqlite3.Row) -> None:
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
    set_setting("active_model", model_id)
    return model_id


def post_model_test(model: sqlite3.Row) -> float:
    provider = model["provider"]
    base_url = model["base_url"].rstrip("/")
    token = decrypt_token(model["token_ciphertext"])
    name = model["name"]
    started = time.monotonic()
    if provider == "anthropic":
        endpoint = base_url if base_url.endswith("/messages") else base_url + "/v1/messages"
        status, _ = http_json(endpoint, "POST",
                              {"model": name, "max_tokens": 8, "messages": [{"role": "user", "content": "Reply OK only."}]},
                              {"x-api-key": token, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, timeout=45)
    elif provider == "google":
        endpoint = base_url + "/models/" + urllib.parse.quote(name, safe="") + ":generateContent"
        endpoint += ("&" if "?" in endpoint else "?") + urllib.parse.urlencode({"key": token})
        status, _ = http_json(endpoint, "POST",
                              {"contents": [{"parts": [{"text": "Reply OK only."}]}], "generationConfig": {"maxOutputTokens": 8}},
                              {"Content-Type": "application/json"}, timeout=45)
    else:
        endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        status, _ = http_json(endpoint, "POST",
                              {"model": name, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8},
                              headers, timeout=45)
    if status < 200 or status >= 300:
        raise RuntimeError("HTTP " + str(status))
    return time.monotonic() - started


# ---------------- github runs (worker-synced) ----------------
def fetch_active_runs() -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for status in ("queued", "in_progress"):
        data = gh("GET", "/actions/workflows/tradingagents.yml/runs?status=" + status + "&per_page=50", timeout=15) or {}
        for run in data.get("workflow_runs", []):
            if run.get("event") == "repository_dispatch":
                active.append(run)
    return list({int(run["id"]): run for run in active if run.get("id")}.values())


def dispatch_run(chat_id: int, mode: str, params: dict[str, Any]) -> str:
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
    data = gh("GET", "/actions/workflows/tradingagents.yml/runs?per_page=100", timeout=15) or {}
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
        text = emoji + " " + label + " <code>" + esc(subject) + "</code> تمام شد.\nنتیجه: <b>" + esc(row["status"]) + "</b>"
        buttons = [[("📄 دیدن خروجی", "view_output:" + row["request_id"])]]
        if row["workflow_run_id"]:
            run_url = "url:https://github.com/" + OWNER + "/" + REPO + "/actions/runs/" + str(row["workflow_run_id"])
            buttons.append([("🔗 صفحه اجرا", run_url)])
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
            lambda: CACHE.update(
                artifacts=(gh("GET", "/actions/artifacts?per_page=15", timeout=15) or {}).get("artifacts", [])
            ),
            notify_finished,
        ):
            with contextlib.suppress(Exception):
                task()
        CACHE["ts"] = time.monotonic()
        time.sleep(20)


def cancel_run(run_id: int) -> None:
    gh("POST", "/actions/runs/" + str(run_id) + "/cancel", timeout=15)


# ---------------- output viewer ----------------
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
        send_message(chat_id, "⏳ هنوز Run ID ثبت نشده؛ چند ثانیه دیگر دوباره امتحان کن.")
        return
    try:
        data = gh("GET", "/actions/runs/" + str(run_id) + "/artifacts") or {}
        arts = [a for a in data.get("artifacts", []) if not a.get("expired")]
        pick = None
        for prefix in ("engine-results-", "engine-run-"):
            for art in arts:
                if str(art.get("name", "")).startswith(prefix):
                    pick = art
                    break
            if pick:
                break
        if not pick:
            send_message(chat_id, "📭 هنوز Artifact خروجی برای این اجرا ساخته نشده است.")
            return
        raw = gh_bytes("/actions/artifacts/" + str(pick["id"]) + "/zip")
    except Exception as exc:
        send_message(chat_id, "❌ دریافت خروجی ناموفق: " + esc(exc))
        return
    zf = zipfile.ZipFile(io.BytesIO(raw))
    scored = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        low = name.lower()
        if not low.endswith((".md", ".txt", ".log", ".json")):
            continue
        try:
            body = zf.read(name).decode("utf-8", "replace")
        except Exception:
            continue
        score = 0
        if "complete_report" in low:
            score += 5
        if "report" in low:
            score += 3
        if low.endswith(".md"):
            score += 2
        if low.endswith("agent.log"):
            score += 1
        scored.append((score, name, body))
    if not scored:
        send_message(chat_id, "📭 خروجی متنی داخل Artifact پیدا نشد.")
        return
    scored.sort(key=lambda item: -item[0])
    caption = "📄 خروجی " + ("تحلیل" if row["mode"] == "analysis" else "بک‌تست") + " — " + scored[0][1]
    tg_document(chat_id, scored[0][1].replace("/", "_"), scored[0][2].encode("utf-8"), caption)
    for _, extra_name, extra_body in scored[1:2]:
        with contextlib.suppress(Exception):
            tg_document(chat_id, extra_name.replace("/", "_"), extra_body.encode("utf-8"), "📎 " + extra_name)


# ---------------- validation ----------------
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
                raise ValueError("Ticker نامعتبر است: " + part)
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


def analysts_names(csv: str) -> str:
    chosen = ", ".join(ANALYST_LABEL[x] for x in ANALYST_ORDER if x in csv.split(","))
    return chosen or "هیچ‌کدام"


# ---------------- ui screens (local-only, no network) ----------------
def build_ui(state: str, payload: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    flash = payload.pop("flash", None)
    head = ("<b>" + esc(flash) + "</b>\n\n") if flash else ""

    if state == "idle":
        aid = active_model_id()
        mrow = q1("SELECT name FROM models WHERE id=?", (aid,)) if aid else None
        model_line = "مدل فعال: <b>" + esc(mrow["name"]) + "</b>" if mrow else "مدل فعال: <b>تنظیم نشده</b>"
        busy = q1("SELECT COUNT(*) c FROM runs WHERE status NOT IN " + str(TERMINAL))["c"]
        live = "\n🟡 اجرای فعال: <b>" + str(busy) + "</b>" if busy else ""
        return (head + "<b>🤖 TradingAgentsArya</b>\n" + model_line + live +
                "\nیک گزینه را انتخاب کن.\n<code>build " + BUILD + "</code>"), [
            [("🚀 تحلیل جدید", "flow_analysis_start"), ("📈 بک‌تست", "flow_backtest_start")],
            [("🤖 مدل‌ها", "models"), ("📊 اجراهای جاری", "active_runs")],
            [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")],
        ]

    if state == "models":
        rows = model_rows()
        aid = active_model_id()
        text = head + "<b>🤖 مدل‌ها</b>\n"
        buttons: list[list[tuple[str, str]]] = []
        if not rows:
            text += "هنوز مدلی ثبت نشده."
        for row in rows:
            mark = "✅ " if row["id"] == aid else ""
            text += "\n" + mark + "<b>" + esc(row["name"]) + "</b> <code>" + esc(row["provider"]) + "</code>"
            buttons.append([("⚡ فعال‌سازی " + row["name"], "activate:" + row["id"])])
        buttons += [
            [("➕ افزودن مدل", "model_add"), ("🧪 تست اتصال", "model_test_menu")],
            [("🗑 حذف مدل", "model_delete_menu")],
            [("🏠 خانه", "home")],
        ]
        return text, buttons

    if state == "model_add_url":
        return head + "<b>➕ افزودن مدل (1/3)</b>\nBase URL را دقیقاً همان‌طور که هست بفرست.", [[("❌ لغو", "wizard_cancel")]]
    if state == "model_add_token":
        return head + "<b>➕ افزودن مدل (2/3)</b>\nAPI Token را بفرست.", [[("❌ لغو", "wizard_cancel")]]
    if state == "model_add_model":
        return head + "<b>➕ افزودن مدل (3/3)</b>\nModel ID دقیق را بفرست؛ بلافاصله ذخیره و فعال می‌شود.", [[("❌ لغو", "wizard_cancel")]]

    if state == "model_test_menu":
        buttons = [[("🧪 " + r["name"], "test_model:" + r["id"])] for r in model_rows()]
        buttons.append([("◀️ مدل‌ها", "models")])
        return head + "<b>🧪 تست اتصال</b>\nیک درخواست بسیار کوچک واقعی به API؛ TradingAgents اجرا نمی‌شود.", buttons

    if state == "model_delete_menu":
        buttons = [[("🗑 " + r["name"], "delete_model:" + r["id"])] for r in model_rows()]
        buttons.append([("◀️ مدل‌ها", "models")])
        return head + "<b>🗑 حذف مدل</b>\nمدل را انتخاب کن.", buttons

    if state == "an_ticker":
        return head + "<b>🔎 تحلیل جدید (مرحله 1)</b>\nTicker را بفرست.\nمثال: <code>NVDA</code>", [[("❌ لغو", "flow_cancel")]]
    if state == "an_date":
        return head + "<b>🔎 تحلیل جدید (مرحله 2)</b>\nتاریخ تحلیل را بفرست یا «امروز» را بزن.", [[("📅 امروز", "analysis_today"), ("❌ لغو", "flow_cancel")]]
    if state == "an_analysts":
        return analyst_ui(payload, head, backtest=False)
    if state == "an_confirm":
        return head + (
            "<b>✅ آماده اجرا</b>\nTicker: <code>" + esc(payload.get("ticker")) + "</code>\n"
            "تاریخ: <code>" + esc(payload.get("date")) + "</code>\n"
            "تحلیلگران: <b>" + esc(analysts_names(payload.get("analysts", ""))) + "</b>"
        ), [
            [("🚀 شروع تحلیل", "analysis_run")],
            [("🎛 تحلیلگران", "analysis_analysts"), ("📅 تغییر تاریخ", "analysis_change_date")],
            [("❌ لغو", "flow_cancel")],
        ]

    if state == "bt_tickers":
        return head + "<b>📈 بک‌تست (مرحله 1)</b>\nTicker یا چند Ticker بفرست.\nمثال: <code>NVDA,AAPL</code>", [[("❌ لغو", "flow_cancel")]]
    if state == "bt_start":
        return head + "<b>📈 بک‌تست (مرحله 2)</b>\nتاریخ شروع را بفرست.\nمثال: <code>2026-01-01</code>", [[("❌ لغو", "flow_cancel")]]
    if state == "bt_end":
        return head + "<b>📈 بک‌تست (مرحله 3)</b>\nتاریخ پایان را بفرست یا «امروز» را بزن.", [[("📅 امروز", "backtest_today"), ("❌ لغو", "flow_cancel")]]
    if state == "bt_every":
        return head + "<b>📈 بک‌تست (مرحله 4)</b>\nفاصله زمانی را انتخاب کن:", [
            [("📅 1 روز", "every:1"), ("📅 7 روز", "every:7")],
            [("📅 14 روز", "every:14"), ("📅 30 روز", "every:30")],
            [("❌ لغو", "flow_cancel")],
        ]
    if state == "bt_analysts":
        return analyst_ui(payload, head, backtest=True)
    if state == "bt_confirm":
        return head + (
            "<b>✅ آماده بک‌تست</b>\nTickerها: <code>" + esc(payload.get("tickers")) + "</code>\n"
            "از <code>" + esc(payload.get("start")) + "</code> تا <code>" + esc(payload.get("end")) + "</code>\n"
            "فاصله: <b>هر " + esc(payload.get("every", 7)) + " روز</b>\n"
            "تحلیلگران: <b>" + esc(analysts_names(payload.get("analysts", ""))) + "</b>"
        ), [
            [("🚀 شروع بک‌تست", "backtest_run")],
            [("🎛 تحلیلگران", "backtest_analysts"), ("📅 فاصله", "backtest_every")],
            [("❌ لغو", "flow_cancel")],
        ]

    if state == "active_runs":
        runs = list(CACHE.get("runs", []))
        lines = [head + "<b>📊 اجراهای جاری</b>"]
        buttons = []
        if runs:
            lines.append("")
            for run in sorted(runs, key=lambda x: str(x.get("created_at", "")), reverse=True):
                lines.append("🟡 <b>" + esc(run.get("display_title") or "Engine") + "</b>")
                buttons.append([("🛑 لغو این اجرا", "cancelw:" + str(run.get("id")))])
        else:
            local = q("SELECT * FROM runs WHERE status NOT IN " + str(TERMINAL) + " ORDER BY created_at DESC LIMIT 5")
            if local:
                lines.append("")
                for row in local:
                    lines.append("🟡 <code>" + esc(row["request_id"][:8]) + "</code> | <b>" + esc(row["status"]) + "</b>")
                    if row["workflow_run_id"]:
                        buttons.append([("🛑 لغو این اجرا", "cancelw:" + str(row["workflow_run_id"]))])
            else:
                lines.append("✅ هیچ پردازش فعالی وجود ندارد.")
        buttons.append([("🔄 تازه‌سازی", "active_runs"), ("🏠 خانه", "home")])
        return "\n".join(lines), buttons

    if state == "history":
        rows = q("SELECT * FROM runs ORDER BY created_at DESC LIMIT 8")
        if not rows:
            return head + "<b>📜 تاریخچه</b>\nهنوز اجرایی ثبت نشده.", [[("🏠 خانه", "home")]]
        lines = [head + "<b>📜 تاریخچه</b>", ""]
        buttons = []
        for row in rows:
            params = json.loads(row["payload_json"] or "{}")
            subject = params.get("ticker") or params.get("tickers") or "—"
            lines.append("• <code>" + esc(row["request_id"][:8]) + "</code> " + esc(subject) + " | <b>" + esc(row["status"]) + "</b>")
            line = [("📄 خروجی " + row["request_id"][:8], "view_output:" + row["request_id"])]
            if row["status"] not in TERMINAL and row["workflow_run_id"]:
                line.append(("🛑 لغو", "cancelw:" + str(row["workflow_run_id"])))
            buttons.append(line)
        buttons.append([("🔄 تازه‌سازی", "history"), ("🏠 خانه", "home")])
        return "\n".join(lines), buttons

    if state == "artifacts":
        items = [a for a in CACHE.get("artifacts", []) if not a.get("expired")][:12]
        if not items:
            text = head + "<b>📦 خروجی‌ها</b>\nخروجی‌ای پیدا نشد."
        else:
            lines = [head + "<b>📦 خروجی‌ها</b>", ""]
            for item in items:
                lines.append("• <code>" + esc(item.get("id")) + "</code> — " + esc(item.get("name")))
            text = "\n".join(lines)
        return text, [[("🔄 تازه‌سازی", "artifacts"), ("🏠 خانه", "home")]]

    return head + "<b>🤖 TradingAgentsArya</b>", [[("🏠 خانه", "home")]]


def analyst_ui(payload: dict, head: str, backtest: bool) -> tuple[str, list[list[tuple[str, str]]]]:
    allowed = list(ANALYST_ORDER)
    ticker = payload.get("ticker", "")
    if not backtest and ticker and is_crypto(ticker):
        allowed.remove("fundamentals")
    selected = {x for x in payload.get("analysts", "").split(",") if x in allowed}
    payload["analysts"] = ",".join(x for x in allowed if x in selected)
    rows = []
    for i in range(0, len(allowed), 2):
        pair = allowed[i:i + 2]
        rows.append([(("✅ " if x in selected else "") + ANALYST_LABEL[x], "toggle_analyst:" + x) for x in pair])
    rows += [[("✅ تایید و ادامه", "analysts_done")], [("❌ لغو", "flow_cancel")]]
    return head + "<b>🎛 تحلیلگران</b>\nموارد انتخاب‌شده با ✅ مشخص‌اند.", rows


# ---------------- text handling ----------------
def handle_text(chat_id: int, text: str) -> None:
    state, payload, _ = get_state(chat_id)
    text = text.strip()
    if text.startswith("/start"):
        set_state(chat_id, "idle", {})
        update_ui(chat_id)
        return
    try:
        if state == "model_add_url":
            url = text.strip()
            if not url.startswith(("http://", "https://")):
                raise ValueError("Base URL باید با http:// یا https:// شروع شود.")
            payload["url"] = url.rstrip("/")
            payload["provider"] = infer_provider(payload["url"])
            set_state(chat_id, "model_add_token", payload)
        elif state == "model_add_token":
            if len(text) < 3:
                raise ValueError("Token خیلی کوتاه است.")
            payload["token"] = text
            set_state(chat_id, "model_add_model", payload)
        elif state == "model_add_model":
            if not text:
                raise ValueError("Model ID نمی‌تواند خالی باشد.")
            add_model(text, payload["provider"], payload["url"], payload.get("token", ""))
            payload = {"flash": "✅ مدل " + text + " ذخیره و فعال شد."}
            set_state(chat_id, "models", payload)
        elif state == "an_ticker":
            payload["ticker"] = valid_ticker(text)
            payload["analysts"] = "market,social,news" if is_crypto(payload["ticker"]) else "market,social,news,fundamentals"
            set_state(chat_id, "an_date", payload)
        elif state == "an_date":
            payload["date"] = valid_date(text)
            set_state(chat_id, "an_analysts", payload)
        elif state == "bt_tickers":
            payload["tickers"] = valid_ticker(text, allow_many=True)
            set_state(chat_id, "bt_start", payload)
        elif state == "bt_start":
            payload["start"] = valid_date(text)
            set_state(chat_id, "bt_end", payload)
        elif state == "bt_end":
            end = valid_date(text)
            if end < payload.get("start", end):
                raise ValueError("تاریخ پایان باید بعد از شروع باشد.")
            payload["end"] = end
            payload.setdefault("every", 7)
            payload["analysts"] = "market,social,news,fundamentals"
            set_state(chat_id, "bt_analysts", payload)
        else:
            return
    except ValueError as exc:
        payload["flash"] = "⚠️ " + str(exc)
        set_state(chat_id, state, payload)
    update_ui(chat_id)


# ---------------- callback handling ----------------
def callback(query: dict[str, Any]) -> None:
    user_id = int(query.get("from", {}).get("id", 0))
    if not authorized(user_id):
        answer_callback(query["id"], "دسترسی مجاز نیست.", True)
        return
    message = query.get("message") or {}
    chat_id = int(message.get("chat", {}).get("id", user_id))
    cb_mid = int(message.get("message_id", 0))
    data = query.get("data", "")
    state, payload, _ = get_state(chat_id)

    answer_callback(query["id"])

    try:
        if data.startswith("view_output:"):
            answer_callback(query["id"], "📄 در حال آماده‌سازی خروجی...")
            threading.Thread(
                target=send_output, args=(chat_id, data.split(":", 1)[1]), daemon=True
            ).start()
            return
        if data.startswith("cancelw:"):
            cancel_run(int(data.split(":", 1)[1]))
            payload["flash"] = "🛑 درخواست لغو ارسال شد."
            set_state(chat_id, "active_runs", payload)
        elif data == "home":
            set_state(chat_id, "idle", {})
        elif data == "models":
            set_state(chat_id, "models", {})
        elif data == "active_runs":
            set_state(chat_id, "active_runs", {})
        elif data == "history":
            set_state(chat_id, "history", {})
        elif data == "artifacts":
            set_state(chat_id, "artifacts", {})
        elif data == "model_add":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل اضافه کند.")
            set_state(chat_id, "model_add_url", {})
        elif data == "wizard_cancel":
            set_state(chat_id, "models", {})
        elif data == "model_test_menu":
            set_state(chat_id, "model_test_menu", {})
        elif data == "model_delete_menu":
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            set_state(chat_id, "model_delete_menu", {})
        elif data.startswith("delete_model:"):
            if not admin(user_id):
                raise ValueError("فقط Admin می‌تواند مدل حذف کند.")
            model_id = data.split(":", 1)[1]
            db("UPDATE models SET enabled=0 WHERE id=?", (model_id,))
            if active_model_id() == model_id:
                set_setting("active_model", "")
            payload = {"flash": "🗑 مدل حذف شد."}
            set_state(chat_id, "models", payload)
        elif data.startswith("activate:"):
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (data.split(":", 1)[1],))
            if not row:
                raise ValueError("مدل پیدا نشد.")
            write_active_model_secrets(row)
            set_setting("active_model", row["id"])
            payload = {"flash": "⚡ مدل " + row["name"] + " فعال شد."}
            set_state(chat_id, "models", payload)
        elif data.startswith("test_model:"):
            row = q1("SELECT * FROM models WHERE id=? AND enabled=1", (data.split(":", 1)[1],))
            if not row:
                raise ValueError("مدل پیدا نشد.")
            try:
                elapsed = post_model_test(row)
                payload["flash"] = "✅ تست موفق — API پاسخ داد (" + format(elapsed, ".1f") + "s)"
            except Exception as exc:
                payload["flash"] = "❌ تست ناموفق — " + str(exc)
            set_state(chat_id, "model_test_menu", payload)
        elif data == "flow_analysis_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            set_state(chat_id, "an_ticker", {})
        elif data == "flow_backtest_start":
            if not active_model_id():
                raise ValueError("اول یک مدل فعال کن.")
            set_state(chat_id, "bt_tickers", {})
        elif data == "flow_cancel":
            set_state(chat_id, "idle", {})
        elif data == "analysis_today":
            payload["date"] = dt.date.today().isoformat()
            set_state(chat_id, "an_analysts", payload)
        elif data == "analysis_change_date":
            set_state(chat_id, "an_date", payload)
        elif data == "backtest_today":
            end = dt.date.today().isoformat()
            if end < payload.get("start", end):
                raise ValueError("امروز قبل از تاریخ شروع است.")
            payload["end"] = end
            payload.setdefault("every", 7)
            payload["analysts"] = "market,social,news,fundamentals"
            set_state(chat_id, "bt_analysts", payload)
        elif data == "backtest_every":
            set_state(chat_id, "bt_every", payload)
        elif data.startswith("every:"):
            payload["every"] = int(data.split(":", 1)[1])
            set_state(chat_id, "bt_analysts", payload)
        elif data.startswith("toggle_analyst:"):
            key = data.split(":", 1)[1]
            selected = {x for x in payload.get("analysts", "").split(",") if x}
            if key in selected:
                selected.remove(key)
            else:
                selected.add(key)
            payload["analysts"] = ",".join(x for x in ANALYST_ORDER if x in selected)
            set_state(chat_id, state, payload)
        elif data == "analysts_done":
            if not payload.get("analysts"):
                raise ValueError("حداقل یک تحلیلگر انتخاب کن.")
            set_state(chat_id, "bt_confirm" if state.startswith("bt_") else "an_confirm", payload)
        elif data in ("analysis_run", "backtest_run"):
            if data == "analysis_run":
                params = {"ticker": payload["ticker"], "date": payload["date"], "analysts": payload.get("analysts", "")}
                mode = "analysis"
            else:
                params = {"tickers": payload["tickers"], "start": payload["start"], "end": payload["end"],
                          "every": int(payload.get("every", 7)), "analysts": payload.get("analysts", "")}
                mode = "backtest"
            request_id = dispatch_run(chat_id, mode, params)
            payload = {"flash": "🚀 در صف اجرا قرار گرفت — کد: " + request_id[:10]}
            set_state(chat_id, "idle", payload)
        else:
            return
    except Exception as exc:
        payload["flash"] = "❌ " + str(exc)
        set_state(chat_id, state if state else "idle", payload)
    update_ui(chat_id, cb_mid)


# ---------------- polling ----------------
def poll() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN:
        raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS:
        raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")
    me = tg("getMe")
    sys.stdout.write("TradingAgents controller started @" + str(me.get("username", "")) + " build " + BUILD + "\n")
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
                    elif (
                        "message" in update
                        and update["message"].get("text")
                        and authorized(int(update["message"]["from"]["id"]))
                    ):
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
                sys.stdout.write("Polling error: " + message + "\n")
                sys.stdout.flush()
                time.sleep(5)


if __name__ == "__main__":
    poll()