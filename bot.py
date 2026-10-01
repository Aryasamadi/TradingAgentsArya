#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single-file Telegram controller for TradingAgentsArya.

The bot is the control plane. GitHub Actions is the execution plane.
Models are added/activated from Telegram and their credentials are stored as
GitHub repository secrets. No model setup is exposed in the workflow UI.
"""
from __future__ import annotations

import base64
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
from pathlib import Path
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
DB.executescript("""
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
""")
DB.commit()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=False)


def http_json(url: str, method: str = "GET", data: Any = None, headers: dict[str, str] | None = None, timeout: int = 60):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    h = {"Accept": "application/vnd.github+json"}
    if headers: h.update(headers)
    if data is not None: h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try: obj = json.loads(raw) if raw else None
        except json.JSONDecodeError: obj = raw
        raise RuntimeError(f"HTTP {e.code}: {obj}") from e


def tg(method: str, payload: dict[str, Any] | None = None):
    status, obj = http_json(f"{TG}/{method}", "POST", payload or {}, {"Content-Type": "application/json"})
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
    if status >= 300: raise RuntimeError(f"GitHub API error {status}: {obj}")
    return obj


def db(sql: str, args: tuple = ()):
    cur = DB.execute(sql, args); DB.commit(); return cur


def send(chat_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)


def edit(chat_id: int, mid: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": mid, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard is not None: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("editMessageText", payload)


def cb(qid: str, text: str = "", alert: bool = False):
    tg("answerCallbackQuery", {"callback_query_id": qid, "text": text[:200], "show_alert": alert})


def kb(rows):
    return [[{"text": a, "callback_data": b} for a, b in row] for row in rows]


def authorized(uid: int) -> bool:
    return uid in ADMIN_IDS or uid in ALLOWED_IDS


def admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def model_rows():
    return DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()


def active_model_id() -> str:
    r = DB.execute("SELECT value FROM settings WHERE key='active_model' ").fetchone()
    return str(r[0]) if r else ""


def set_active(mid: str):
    db("INSERT INTO settings(key,value) VALUES('active_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mid,))


def github_secret(name: str, value: str):
    # GitHub repository secrets use the repository public key + libsodium sealed boxes.
    try:
        from nacl import encoding, public
    except ImportError:
        raise RuntimeError("PyNaCl is required for model-secret management. Install pynacl in the bot environment.")
    key = gh("GET", "/actions/secrets/public-key")
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    encrypted = public.SealedBox(pk).encrypt(value.encode())
    gh("PUT", f"/actions/secrets/{urllib.parse.quote(name, safe='')}", {
        "encrypted_value": base64.b64encode(encrypted).decode(), "key_id": key["key_id"]
    })


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



def activate_model(mid: str):
    r = DB.execute("SELECT * FROM models WHERE id=? AND enabled=1", (mid,)).fetchone()
    if not r: raise ValueError("مدل پیدا نشد.")
    provider = r["provider"].lower().strip()
    secret = PROVIDER_SECRET.get(provider)
    if provider not in PROVIDER_SECRET and provider not in NO_KEY_PROVIDERS:
        raise ValueError(f"Provider پشتیبانی‌شده نیست: {provider}")
    if secret:
        if not r["api_key"]: raise ValueError("API Key این مدل ثبت نشده است.")
        github_secret(secret, r["api_key"])
    if provider == "ollama":
        github_secret("OLLAMA_BASE_URL", r["base_url"] or "http://localhost:11434/v1")
    elif provider == "bedrock":
        if not r["region"]: raise ValueError("برای Bedrock باید region ثبت شود.")
        github_secret("AWS_DEFAULT_REGION", r["region"])
    github_secret("TRADINGAGENTS_LLM_PROVIDER", provider)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM", r["model"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM", r["model"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL", r["base_url"] if provider not in {"ollama"} else "")
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED", "true")
    set_active(mid)
    return r


def dispatch_run(mode: str, *, ticker: str = "", date: str = "", tickers: str = "", start: str = "", end: str = "", every: int = 7, analysts: str = "", portfolio_json: str = "", run_id: str = ""):
    if not active_model_id():
        raise ValueError("هنوز هیچ مدل فعالی ثبت نشده است. اول از بخش مدل‌ها یک مدل اضافه و فعال کن.")
    mid = active_model_id(); activate_model(mid)
    rid = uuid.uuid4().hex
    payload = {
        "mode": mode, "ticker": ticker.upper().strip(), "date": date.strip(),
        "tickers": tickers.strip(), "start": start.strip(), "end": end.strip(),
        "every": str(every), "analysts": analysts.strip(), "portfolio_json": portfolio_json,
        "request_id": rid, "run_id": run_id.strip(),
    }
    gh("POST", "/dispatches", {"event_type": "tradingagents_run", "client_payload": payload})
    display_ticker = payload["ticker"] or payload["tickers"]
    display_date = payload["date"] or f"{payload['start']} → {payload['end']}"
    db("INSERT INTO runs(request_id,ticker,date,analysts,model_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
       (rid,display_ticker,display_date,payload["analysts"],mid,"queued",now(),now()))
    return rid, mid


def list_runs():
    x = gh("GET", "/actions/runs?per_page=15")
    return x.get("workflow_runs", []) if isinstance(x, dict) else []


def main_menu(cid: int):
    send(cid, "<b>TradingAgents</b>\nکنترلر آماده است.", kb([
        [("▶️ اجرای تحلیل", "run"), ("📊 وضعیت", "status")],
        [("🤖 مدل‌ها", "models"), ("🧪 تست مدل", "tests")],
        [("📜 تاریخچه", "history"), ("📦 خروجی‌ها", "artifacts")],
    ]))


def model_menu(cid: int):
    rows = model_rows(); active = active_model_id()
    text = "<b>مدل‌ها</b>\n"
    if not rows: text += "هنوز مدلی ثبت نشده است."
    for r in rows:
        mark = " ✅" if r["id"] == active else ""
        text += f"• <code>{esc(r['id'])}</code> — {esc(r['name'])} — {esc(r['provider'])}/{esc(r['model'])}{mark}\n"
    rows_kb = [[("➕ افزودن مدل", "add")]]
    for r in rows: rows_kb.append([(f"▶️ فعال: {r['name']}", f"activate:{r['id']}")])
    rows_kb += [[("🗑 حذف مدل", "delmenu")], [("◀️ خانه", "home")]]
    send(cid, text, kb(rows_kb))


def run_menu(cid: int):
    if not model_rows():
        send(cid, "⚠️ ابتدا از بخش <b>مدل‌ها</b> یک مدل اضافه و فعال کن.", kb([[('🤖 مدل‌ها', 'models')]])); return
    text = (
        "<b>اجرای تحلیل</b>\n"
        "تحلیل تکی: <code>/analyze NVDA 2026-09-30</code>\n"
        "بک‌تست: <code>/backtest NVDA,AAPL 2026-01-01 2026-09-30 7</code>\n\n"
        "تحلیل‌گرهای خاص را می‌توانی در انتهای دستور بنویسی: "
        "<code>market,news,fundamentals,social</code>"
    )
    send(cid, text, kb([[('◀️ خانه', 'home')]]))


def status(cid: int):
    runs = list_runs(); lines = ["<b>وضعیت اجراها</b>"]
    for r in runs[:8]: lines.append(f"• <code>{r.get('id')}</code> — {esc(r.get('status'))}/{esc(r.get('conclusion') or 'running')}")
    send(cid, "\n".join(lines), kb([[("🔄 تازه‌سازی", "status"),("◀️ خانه", "home")]]))


def history(cid: int):
    rows = DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 12").fetchall()
    text = "<b>تاریخچه</b>\n" + ("هنوز اجرا نداریم." if not rows else "\n".join(f"• <code>{esc(r['request_id'][:10])}</code> {esc(r['ticker'])} — {esc(r['status'])}" for r in rows))
    send(cid, text, kb([[("◀️ خانه", "home")]]))


def artifacts(cid: int):
    data = gh("GET", "/actions/artifacts?per_page=20"); arr = data.get("artifacts", []) if isinstance(data,dict) else []
    text = "<b>Artifactها</b>\n" + ("موردی نیست." if not arr else "\n".join(f"• <code>{a.get('id')}</code> — {esc(a.get('name'))}" for a in arr[:12]))
    send(cid, text, kb([[("🔄 تازه‌سازی", "artifacts"),("◀️ خانه", "home")]]))


def add_model_prompt(cid: int):
    send(cid, "مدل را در یک پیام JSON بفرست:\n<code>{\"id\":\"nvidia\",\"name\":\"NVIDIA GPT\",\"provider\":\"nvidia\",\"model\":\"openai/gpt-oss-20b\",\"base_url\":\"\",\"api_key\":\"YOUR_KEY\"}</code>\n\nAPI Key فقط برای ذخیره در GitHub Secret استفاده می‌شود و در پیام‌های بعدی نمایش داده نمی‌شود.", kb([[("◀️ مدل‌ها", "models")]]))


def handle_command(m: dict):
    cid = int(m["chat"]["id"]); uid = int(m["from"]["id"]); text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        main_menu(cid); return
    if text.startswith("/analyze"):
        p = text.split()
        if len(p) < 3: send(cid, "فرمت: <code>/analyze TICKER YYYY-MM-DD [analysts]</code>"); return
        try:
            rid, mid = dispatch_run("analysis", ticker=p[1], date=p[2], analysts=p[3] if len(p) > 3 else "")
            send(cid, f"✅ تحلیل ارسال شد.\nمدل: <code>{esc(mid)}</code>\nRequest: <code>{rid}</code>")
        except Exception as e: send(cid, f"❌ {esc(e)}")
        return
    if text.startswith("/backtest"):
        p = text.split()
        if len(p) < 4: send(cid, "فرمت: <code>/backtest TICKERS START END [every] [analysts]</code>"); return
        try:
            every = int(p[4]) if len(p) > 4 else 7
            analysts = p[5] if len(p) > 5 else ""
            rid, mid = dispatch_run("backtest", tickers=p[1], start=p[2], end=p[3], every=every, analysts=analysts)
            send(cid, f"✅ بک‌تست ارسال شد.\nمدل: <code>{esc(mid)}</code>\nRequest: <code>{rid}</code>")
        except Exception as e: send(cid, f"❌ {esc(e)}")
        return
    # JSON model creation is intentionally admin-only.
    if text.startswith("{") and admin(uid):
        try:
            x = json.loads(text)
            try:
                tg("deleteMessage", {"chat_id": cid, "message_id": int(m.get("message_id", 0))})
            except Exception:
                pass
            required = ["id","name","provider","model"]
            if any(not str(x.get(k,"" )).strip() for k in required): raise ValueError("id/name/provider/model الزامی است.")
            ts = now(); db("INSERT INTO models(id,name,provider,model,base_url,region,api_key,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,provider=excluded.provider,model=excluded.model,base_url=excluded.base_url,region=excluded.region,api_key=excluded.api_key,enabled=1,updated_at=excluded.updated_at", (x["id"],x["name"],x["provider"],x["model"],x.get("base_url",""),x.get("region",""),x.get("api_key",""),ts,ts))
            activate_model(x["id"])
            send(cid, "✅ مدل ثبت و فعال شد. تنظیمات امن آن در GitHub Secrets قرار گرفت.", kb([[ ("▶️ اجرای تحلیل", "run"),("🧪 تست مدل", "tests")],[ ("🤖 مدل‌ها", "models")]]))
        except Exception as e: send(cid, f"❌ JSON مدل نامعتبر است:\n<code>{esc(e)}</code>")


def test_menu(cid: int):
    rows = model_rows()
    if not rows:
        send(cid, "⚠️ هنوز مدلی ثبت نشده است.", kb([[('🤖 مدل‌ها', 'models')]]))
        return
    rows_kb = [[(f"🧪 تست {r['name']}", f"test:{r['id']}" )] for r in rows]
    rows_kb.append([("◀️ خانه", "home")])
    send(cid, "مدل موردنظر برای تست را انتخاب کن:", kb(rows_kb))

def launch_test(cid: int, mid: str):
    try:
        set_active(mid)
        rid, active = dispatch_run(
            "analysis", ticker="AAPL", date=dt.date.today().isoformat(),
            analysts="market"
        )
        send(cid, f"🧪 تست ارسال شد.\nمدل: <code>{esc(active)}</code>\nRequest: <code>{rid}</code>", kb([[('📊 وضعیت','status'),('◀️ خانه','home')]]))
    except Exception as e:
        send(cid, f"❌ تست ناموفق بود:\n<code>{esc(e)}</code>", kb([[('🤖 مدل‌ها','models')]]))

def callback(q: dict):
    uid = int(q.get("from",{}).get("id",0)); cid = int(q.get("message",{}).get("chat",{}).get("id",uid)); data = q.get("data","")
    if not authorized(uid): cb(q["id"], "دسترسی مجاز نیست.", True); return
    cb(q["id"])
    if data == "home": main_menu(cid)
    elif data == "models": model_menu(cid)
    elif data == "add": add_model_prompt(cid)
    elif data.startswith("activate:"):
        if not admin(uid): send(cid,"فقط ادمین می‌تواند مدل را فعال کند."); return
        try: activate_model(data.split(":",1)[1]); send(cid,"✅ مدل فعال شد و تنظیمات آن در GitHub Secrets قرار گرفت.", kb([[("▶️ اجرای تحلیل", "run"),("◀️ خانه", "home")]]))
        except Exception as e: send(cid,f"❌ فعال‌سازی ناموفق:\n<code>{esc(e)}</code>")
    elif data == "run": run_menu(cid)
    elif data == "status": status(cid)
    elif data == "history": history(cid)
    elif data == "artifacts": artifacts(cid)
    elif data == "tests": test_menu(cid)
    elif data.startswith("test:"):
        if not admin(uid):
            send(cid, "⛔ فقط Admin می‌تواند تست مدل اجرا کند.")
            return
        launch_test(cid, data.split(":",1)[1])
    elif data == "delmenu":
        rows = model_rows(); send(cid,"مدل را برای حذف انتخاب کن.", kb([[ (f"🗑 {r['name']}", f"delete:{r['id']}") ] for r in rows] + [[("◀️ مدل‌ها","models")]]))
    elif data.startswith("delete:"):
        if not admin(uid): return
        mid=data.split(":",1)[1]; db("DELETE FROM models WHERE id=?",(mid,));
        if active_model_id()==mid: db("DELETE FROM settings WHERE key='active_model'")
        send(cid,"✅ مدل حذف شد.", kb([[("🤖 مدل‌ها","models")]]))


def poll():
    if not BOT_TOKEN: raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN: raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS: raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")
    offset = 0; me = tg("getMe"); print(f"TradingAgents bot started @{me.get('username','')}", flush=True)
    while True:
        try:
            updates = tg("getUpdates", {"offset":offset,"timeout":50,"allowed_updates":["message","callback_query"]}) or []
            for u in updates:
                offset = int(u["update_id"])+1
                try:
                    if "callback_query" in u: callback(u["callback_query"])
                    elif "message" in u and authorized(int(u["message"].get("from",{}).get("id",0))): handle_command(u["message"])
                except Exception:
                    traceback.print_exc()
        except Exception as e:
            print(f"Polling error: {e}", flush=True); time.sleep(5)

if __name__ == "__main__": poll()
