#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradingAgentsArya Telegram controller.

Telegram = control/monitoring layer.
GitHub Actions = execution layer.
SQLite state is restored/saved by the workflow artifact.
"""
from __future__ import annotations
import base64, datetime as dt, hashlib, html, json, os, sqlite3, time, traceback, urllib.error, urllib.parse, urllib.request, uuid
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
 base_url TEXT DEFAULT '', region TEXT DEFAULT '', token_ciphertext BLOB,
 enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs(
 request_id TEXT PRIMARY KEY, workflow_run_id INTEGER, ticker TEXT NOT NULL, date TEXT NOT NULL,
 analysts TEXT DEFAULT '', model_id TEXT DEFAULT '', status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wizard(
 chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, stage TEXT NOT NULL,
 base_url TEXT DEFAULT '', token TEXT DEFAULT '', model_name TEXT DEFAULT '',
 provider TEXT DEFAULT 'openai_compatible', prompt_message_id INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flows(
 chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, stage TEXT NOT NULL,
 data_json TEXT NOT NULL DEFAULT '{}', prompt_message_id INTEGER, updated_at TEXT NOT NULL
);
""")
DB.commit()
try:
    DB.execute("ALTER TABLE models ADD COLUMN token_ciphertext BLOB DEFAULT NULL")
    DB.commit()
except sqlite3.OperationalError:
    pass


def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def esc(v): return html.escape(str(v if v is not None else ""), quote=False)

def http_json(url: str, method="GET", data=None, headers=None, timeout=60):
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
        except Exception: obj = raw
        raise RuntimeError(f"HTTP {e.code}: {obj}") from e

def tg(method, payload=None):
    _, obj = http_json(f"{TG}/{method}", "POST", payload or {}, {"Content-Type":"application/json"})
    if not isinstance(obj, dict) or not obj.get("ok"):
        raise RuntimeError(f"Telegram API error: {obj}")
    return obj.get("result")

def gh(method, path, data=None):
    if not GH_TOKEN: raise RuntimeError("BOT_GITHUB_TOKEN is required")
    h = {"Authorization":f"Bearer {GH_TOKEN}", "X-GitHub-Api-Version":"2026-03-10", "Accept":"application/vnd.github+json", "User-Agent":"TradingAgentsArya-Telegram"}
    status, obj = http_json(GH + path, method, data, h)
    if status >= 300: raise RuntimeError(f"GitHub API error {status}: {obj}")
    return obj

def db(sql, args=()):
    cur = DB.execute(sql, args); DB.commit(); return cur

def send(chat_id, text, keyboard=None):
    p={"chat_id":chat_id,"text":text[:4096],"parse_mode":"HTML","disable_web_page_preview":True}
    if keyboard is not None: p["reply_markup"]={"inline_keyboard":keyboard}
    return tg("sendMessage",p)

def edit(chat_id, message_id, text, keyboard=None):
    p={"chat_id":chat_id,"message_id":message_id,"text":text[:4096],"parse_mode":"HTML","disable_web_page_preview":True}
    if keyboard is not None: p["reply_markup"]={"inline_keyboard":keyboard}
    try: return tg("editMessageText",p)
    except Exception as e:
        if "message is not modified" in str(e).lower(): return None
        raise

def answer_cb(qid, text="", alert=False): tg("answerCallbackQuery", {"callback_query_id":qid,"text":text[:200],"show_alert":alert})
def kb(rows): return [[{"text":a,"callback_data":b} for a,b in row] for row in rows]
def authorized(uid): return uid in ADMIN_IDS or uid in ALLOWED_IDS
def admin(uid): return uid in ADMIN_IDS

# ---------- secrets / models ----------
def token_box():
    from nacl.secret import SecretBox
    key=hashlib.sha256((OWNER+":"+REPO+":"+GH_TOKEN).encode()).digest()
    return SecretBox(key)
def encrypt_token(v): return bytes(token_box().encrypt(v.encode()))
def decrypt_token(v): return token_box().decrypt(bytes(v)).decode() if v else ""

def github_secret(name, value):
    from nacl import encoding, public
    key=gh("GET","/actions/secrets/public-key")
    pub=public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    enc=public.SealedBox(pub).encrypt(value.encode())
    gh("PUT",f"/actions/secrets/{urllib.parse.quote(name,safe='')}",{"encrypted_value":base64.b64encode(enc).decode(),"key_id":key["key_id"]})

PROVIDER_SECRET={
 "openai":"OPENAI_API_KEY","anthropic":"ANTHROPIC_API_KEY","google":"GOOGLE_API_KEY","azure":"AZURE_OPENAI_API_KEY",
 "xai":"XAI_API_KEY","deepseek":"DEEPSEEK_API_KEY","qwen":"DASHSCOPE_API_KEY","qwen-cn":"DASHSCOPE_CN_API_KEY",
 "glm":"ZHIPU_API_KEY","glm-cn":"ZHIPU_CN_API_KEY","minimax":"MINIMAX_API_KEY","minimax-cn":"MINIMAX_CN_API_KEY",
 "openrouter":"OPENROUTER_API_KEY","mistral":"MISTRAL_API_KEY","kimi":"MOONSHOT_API_KEY","groq":"GROQ_API_KEY",
 "nvidia":"NVIDIA_API_KEY","openai_compatible":"OPENAI_COMPATIBLE_API_KEY"
}
NO_KEY={"bedrock","ollama"}

def infer_provider(url):
    host=urllib.parse.urlparse(url).netloc.lower()
    for needle,p in [("api.openai.com","openai"),("api.anthropic.com","anthropic"),("generativelanguage.googleapis.com","google"),("api.x.ai","xai"),("api.deepseek.com","deepseek"),("openrouter.ai","openrouter"),("api.mistral.ai","mistral"),("api.groq.com","groq"),("api.moonshot.ai","kimi"),("api.minimax.io","minimax")]:
        if needle in host: return p
    return "openai_compatible"

def models(): return DB.execute("SELECT * FROM models WHERE enabled=1 ORDER BY name COLLATE NOCASE").fetchall()
def active_id():
    r=DB.execute("SELECT value FROM settings WHERE key='active_model'").fetchone(); return str(r[0]) if r else ""
def set_active(mid): db("INSERT INTO settings(key,value) VALUES('active_model',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mid,))

def activate(mid):
    r=DB.execute("SELECT * FROM models WHERE id=? AND enabled=1",(mid,)).fetchone()
    if not r: raise ValueError("مدل پیدا نشد.")
    p=r["provider"].strip().lower()
    if p not in PROVIDER_SECRET and p not in NO_KEY: raise ValueError(f"Provider پشتیبانی نمی‌شود: {p}")
    if p in PROVIDER_SECRET:
        tok=decrypt_token(r["token_ciphertext"])
        if not tok: raise ValueError("توکن مدل موجود نیست؛ مدل را دوباره اضافه کن.")
        github_secret(PROVIDER_SECRET[p],tok)
    if p=="ollama": github_secret("OLLAMA_BASE_URL",r["base_url"] or "http://localhost:11434/v1")
    if p=="bedrock" and r["region"]: github_secret("AWS_DEFAULT_REGION",r["region"])
    github_secret("TRADINGAGENTS_LLM_PROVIDER",p)
    github_secret("TRADINGAGENTS_DEEP_THINK_LLM",r["model"])
    github_secret("TRADINGAGENTS_QUICK_THINK_LLM",r["model"])
    github_secret("TRADINGAGENTS_LLM_BACKEND_URL",r["base_url"] if p!="ollama" else "")
    github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED","true")
    set_active(mid)
    return r

# ---------- execution ----------
def dispatch_run(mode, ticker="", date="", tickers="", start="", end="", every=7, analysts="", portfolio_json=""):
    mid=active_id()
    if not mid: raise ValueError("هیچ مدل فعالی نداریم. اول یک مدل اضافه و فعال کن.")
    rid=uuid.uuid4().hex
    payload={"mode":mode,"ticker":ticker.upper().strip(),"date":date.strip(),"tickers":tickers.strip(),"start":start.strip(),"end":end.strip(),"every":str(every),"analysts":analysts.strip(),"portfolio_json":portfolio_json,"request_id":rid}
    try:
        gh("POST","/dispatches",{"event_type":"tradingagents_run","client_payload":payload})
    except Exception as first:
        # Fallback: same workflow through workflow_dispatch. Requires Actions: write on BOT_GITHUB_TOKEN.
        try:
            gh("POST",f"/actions/workflows/tradingagents.yml/dispatches",{"ref":REF,"inputs":{"mode":"engine","payload":json.dumps(payload,ensure_ascii=False)}})
        except Exception as second:
            raise RuntimeError(f"ارسال Engine شکست خورد. repository_dispatch: {first}; workflow_dispatch fallback: {second}")
    display=ticker.upper().strip() or tickers.strip()
    display_date=date.strip() or f"{start.strip()} → {end.strip()}"
    db("INSERT INTO runs(request_id,ticker,date,analysts,model_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(rid,display,display_date,analysts.strip(),mid,"queued",now(),now()))
    return rid,mid

def workflow_runs():
    x=gh("GET","/actions/runs?per_page=20")
    return x.get("workflow_runs",[]) if isinstance(x,dict) else []

def sync_run_status():
    rows=DB.execute("SELECT * FROM runs WHERE status NOT IN ('completed','failure','cancelled','skipped') ORDER BY created_at DESC LIMIT 20").fetchall()
    runs=workflow_runs()
    for row in rows:
        candidates=[r for r in runs if r.get("created_at","") >= row["created_at"][:19]]
        if not candidates: continue
        # Best available correlation: workflow run created after request. The exact request id is also in engine logs/artifacts.
        r=candidates[0]
        status=r.get("status") or "queued"; conclusion=r.get("conclusion")
        new=(conclusion if status=="completed" and conclusion else status)
        if new != row["status"]:
            db("UPDATE runs SET workflow_run_id=?,status=?,updated_at=? WHERE request_id=?",(r.get("id"),new,now(),row["request_id"]))

def status_text():
    try: sync_run_status()
    except Exception: pass
    rs=DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 10").fetchall()
    if not rs: return "<b>📊 وضعیت اجراها</b>\nاجرایی ثبت نشده است."
    lines=["<b>📊 وضعیت اجراها</b>"]
    for r in rs:
        lines.append(f"• <code>{esc(r['request_id'][:10])}</code> — {esc(r['ticker'])} — <b>{esc(r['status'])}</b>")
    return "\n".join(lines)

# ---------- menus ----------
def menu(chat,text,rows,message_id=None):
    if message_id:
        try: edit(chat,message_id,text,kb(rows)); return message_id
        except Exception: pass
    return int(send(chat,text,kb(rows))["message_id"])

def main_menu(chat,message_id=None):
    return menu(chat,"<b>🤖 TradingAgentsArya</b>\nکنترل کامل از Telegram؛ اجرا روی GitHub Actions.",[
        [("▶️ اجرا","run"),("📊 وضعیت","status")],
        [("🤖 مدل‌ها","models"),("🧪 تست","tests")],
        [("📜 تاریخچه","history"),("📦 خروجی‌ها","artifacts")],
    ],message_id)

def model_menu(chat,message_id=None):
    rows=models(); aid=active_id(); lines=["<b>🤖 مدل‌ها</b>"]
    buttons=[]
    if not rows: lines.append("هنوز مدلی ثبت نشده است.")
    for r in rows:
        mark=" ✅ فعال" if r["id"]==aid else ""
        lines.append(f"• <code>{esc(r['name'])}</code> — {esc(r['provider'])}{mark}")
        buttons.append([(f"⚡ فعال‌سازی {r['name']}",f"activate:{r['id']}")])
    buttons += [[("➕ افزودن مدل","add")],[ ("🗑 حذف مدل","delmenu") ],[("◀️ خانه","home")]]
    return menu(chat,"\n".join(lines),buttons,message_id)

def run_menu(chat,message_id=None):
    return menu(chat,"<b>▶️ اجرای جدید</b>\nفقط مرحله‌به‌مرحله جواب بده؛ نیازی به نوشتن دستور یا فرمت خاص نیست.",[
        [("🔎 تحلیل جدید","new_analysis")],[("📈 بک‌تست جدید","new_backtest")],
        [("📊 وضعیت","status"),("🏠 خانه","home")]
    ],message_id)

def history_menu(chat,message_id=None):
    rs=DB.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 12").fetchall()
    text="<b>📜 تاریخچه</b>\n"+("اجرایی ثبت نشده است." if not rs else "\n".join(f"• <code>{esc(r['request_id'][:10])}</code> — {esc(r['ticker'])} — {esc(r['status'])}" for r in rs))
    return menu(chat,text,[[ ("🔄 تازه‌سازی","history"),("◀️ خانه","home") ]],message_id)

def artifacts_menu(chat,message_id=None):
    data=gh("GET","/actions/artifacts?per_page=20"); items=data.get("artifacts",[])
    text="<b>📦 خروجی‌ها</b>\n"+("موردی نیست." if not items else "\n".join(f"• <code>{a.get('id')}</code> — {esc(a.get('name'))}" for a in items[:15]))
    return menu(chat,text,[[ ("🔄 تازه‌سازی","artifacts"),("◀️ خانه","home") ]],message_id)

def test_menu(chat,message_id=None):
    rs=models(); buttons=[[(f"🧪 {r['name']}",f"test:{r['id']}")] for r in rs]
    buttons.append([("◀️ خانه","home")])
    return menu(chat,"<b>🧪 تست مدل</b>\nتست، یک اجرای واقعی کوتاه با همان مدل فعال است.",buttons,message_id)

def delete_menu(chat,message_id=None):
    rs=models(); buttons=[[(f"🗑 {r['name']}",f"delete:{r['id']}")] for r in rs]; buttons.append([("◀️ مدل‌ها","models")])
    return menu(chat,"<b>حذف مدل</b>\nمدل را انتخاب کن:",buttons,message_id)

# ---------- guided execution flow ----------
def flow_row(chat):
    return DB.execute("SELECT * FROM flows WHERE chat_id=?", (chat,)).fetchone()

def flow_save(chat, uid, kind, stage, data=None, mid=None):
    payload=json.dumps(data or {}, ensure_ascii=False)
    db("INSERT INTO flows(chat_id,user_id,kind,stage,data_json,prompt_message_id,updated_at) VALUES(?,?,?,?,?,?,?) "
       "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,kind=excluded.kind,stage=excluded.stage,data_json=excluded.data_json,prompt_message_id=excluded.prompt_message_id,updated_at=excluded.updated_at",
       (chat,uid,kind,stage,payload,mid,now()))

def flow_clear(chat):
    db("DELETE FROM flows WHERE chat_id=?", (chat,))

def flow_data(w):
    try: return json.loads(w["data_json"] or "{}")
    except Exception: return {}

def flow_prompt(chat,uid,kind,stage,text,data=None,mid=None,rows=None):
    rows=rows or [[("❌ لغو","flow_cancel")]]
    if mid:
        edit(chat,mid,text,kb(rows)); flow_save(chat,uid,kind,stage,data,mid); return mid
    n=int(send(chat,text,kb(rows))["message_id"]); flow_save(chat,uid,kind,stage,data,n); return n

def valid_date(text):
    text=text.strip().lower()
    if text in ("today","امروز"):
        return dt.date.today().isoformat()
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError("تاریخ را به شکل YYYY-MM-DD بفرست؛ مثلاً 2026-09-30")

def start_analysis_flow(chat,uid,mid=None):
    flow_clear(chat)
    return flow_prompt(chat,uid,"analysis","ticker",
        "<b>🔎 تحلیل جدید</b>\n\n<b>1 از 3</b> — نماد را بفرست.\nمثال: <code>NVDA</code>",{},mid)

def start_backtest_flow(chat,uid,mid=None):
    flow_clear(chat)
    return flow_prompt(chat,uid,"backtest","tickers",
        "<b>📈 بک‌تست جدید</b>\n\n<b>1 از 4</b> — نماد یا نمادها را بفرست.\nمثال: <code>NVDA,AAPL</code>\nمی‌توانی با فاصله هم بنویسی.",{},mid)

def flow_finish_analysis(chat,mid,data):
    rid,model=dispatch_run("analysis",ticker=data["ticker"],date=data["date"],analysts=data.get("analysts", ""))
    flow_clear(chat)
    edit(chat,mid,
         f"<b>🚀 تحلیل ارسال شد</b>\n\nنماد: <code>{esc(data['ticker'])}</code>\nتاریخ: <code>{esc(data['date'])}</code>\nمدل: <code>{esc(model)}</code>\nشناسه: <code>{esc(rid)}</code>",
         kb([[('📊 وضعیت','status'),('📜 تاریخچه','history')],[('🏠 خانه','home')]]))

def flow_finish_backtest(chat,mid,data):
    rid,model=dispatch_run("backtest",tickers=data["tickers"],start=data["start"],end=data["end"],every=int(data.get("every",7)),analysts=data.get("analysts", ""))
    flow_clear(chat)
    edit(chat,mid,
         f"<b>🚀 بک‌تست ارسال شد</b>\n\nنمادها: <code>{esc(data['tickers'])}</code>\nبازه: <code>{esc(data['start'])}</code> تا <code>{esc(data['end'])}</code>\nفاصله: <code>{esc(data.get('every',7))} روز</code>\nمدل: <code>{esc(model)}</code>\nشناسه: <code>{esc(rid)}</code>",
         kb([[('📊 وضعیت','status'),('📜 تاریخچه','history')],[('🏠 خانه','home')]]))

def handle_flow(message):
    chat=int(message["chat"]["id"]); uid=int(message["from"]["id"]); w=flow_row(chat)
    if not w or w["user_id"]!=uid or not authorized(uid): return False
    text=(message.get("text") or "").strip(); mid=w["prompt_message_id"]; data=flow_data(w)
    if not text: return True
    try:
        if w["kind"]=="analysis":
            if w["stage"]=="ticker":
                t=text.upper().replace("$","").strip()
                if not t or any(c.isspace() for c in t): raise ValueError("فقط یک نماد وارد کن؛ مثلاً NVDA")
                data["ticker"]=t
                flow_prompt(chat,uid,"analysis","date","<b>2 از 3</b> — تاریخ تحلیل را بفرست.\nمثال: <code>2026-09-30</code> یا <code>امروز</code>",data,mid)
                return True
            if w["stage"]=="date":
                data["date"]=valid_date(text)
                flow_prompt(chat,uid,"analysis","analysts","<b>3 از 3</b> — نوع تحلیل را انتخاب کن.",data,mid,[[('🧠 همه تحلیلگران','flow_analysis_all')],[('🎯 انتخاب تحلیلگران','flow_analysis_pick')],[('❌ لغو','flow_cancel')]])
                return True
        if w["kind"]=="backtest":
            if w["stage"]=="tickers":
                parts=[x.strip().upper().replace("$","") for x in text.replace(","," ").split() if x.strip()]
                if not parts: raise ValueError("حداقل یک نماد وارد کن؛ مثلاً NVDA,AAPL")
                data["tickers"]=",".join(dict.fromkeys(parts))
                flow_prompt(chat,uid,"backtest","start","<b>2 از 4</b> — تاریخ شروع را بفرست.\nمثال: <code>2026-01-01</code>",data,mid)
                return True
            if w["stage"]=="start":
                data["start"]=valid_date(text)
                flow_prompt(chat,uid,"backtest","end","<b>3 از 4</b> — تاریخ پایان را بفرست.\nمثال: <code>2026-09-30</code>",data,mid)
                return True
            if w["stage"]=="end":
                data["end"]=valid_date(text)
                if data["end"] < data["start"]: raise ValueError("تاریخ پایان باید بعد از تاریخ شروع باشد.")
                flow_prompt(chat,uid,"backtest","every","<b>4 از 4</b> — فاصله تحلیل‌ها را انتخاب کن.",data,mid,[[('📅 هر 1 روز','flow_every:1'),('📅 هر 7 روز','flow_every:7')],[('📅 هر 14 روز','flow_every:14'),('📅 هر 30 روز','flow_every:30')],[('❌ لغو','flow_cancel')]])
                return True
    except Exception as e:
        edit(chat,mid,f"❌ {esc(e)}\n\nدوباره همین مرحله را وارد کن.",kb([[('❌ لغو','flow_cancel')]]))
    return True

# ---------- model wizard ----------
def wrow(chat): return DB.execute("SELECT * FROM wizard WHERE chat_id=?",(chat,)).fetchone()
def wsave(chat,uid,stage,base_url="",token="",model_name="",provider="openai_compatible",mid=None):
    db("INSERT INTO wizard(chat_id,user_id,stage,base_url,token,model_name,provider,prompt_message_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id,stage=excluded.stage,base_url=excluded.base_url,token=excluded.token,model_name=excluded.model_name,provider=excluded.provider,prompt_message_id=excluded.prompt_message_id,updated_at=excluded.updated_at",(chat,uid,stage,base_url,token,model_name,provider,mid,now()))
def wclear(chat): db("DELETE FROM wizard WHERE chat_id=?",(chat,))
def wprompt(chat,uid,stage,text,base_url="",token="",model_name="",provider="openai_compatible",mid=None):
    buttons=[[{"text":"❌ لغو","callback_data":"wizard_cancel"}]]
    if mid:
        edit(chat,mid,text,buttons); wsave(chat,uid,stage,base_url,token,model_name,provider,mid); return mid
    n=int(send(chat,text,buttons)["message_id"]); wsave(chat,uid,stage,base_url,token,model_name,provider,n); return n

def start_wizard(chat,uid,mid=None):
    return wprompt(chat,uid,"base_url","<b>➕ افزودن مدل</b>\n\n1/3 — Base URL را بفرست.\nمثال: <code>https://integrate.api.nvidia.com/v1</code>\nاگر لازم نیست: <code>-</code>",mid=mid)

def finish_model(chat,w,provider):
    name=w["model_name"].strip(); url="" if w["base_url"]=="-" else w["base_url"].strip(); token=w["token"].strip()
    if not name or not token: raise ValueError("Model name و Token الزامی هستند.")
    if provider not in PROVIDER_SECRET and provider not in NO_KEY: provider="openai_compatible"
    if provider not in PROVIDER_SECRET: raise ValueError("این نسخه برای Bedrock/Ollama نیاز به تنظیمات اختصاصی دارد؛ فعلاً OpenAI-compatible را انتخاب کن.")
    mid="m_"+uuid.uuid4().hex[:10]; ts=now()
    db("INSERT INTO models(id,name,provider,model,base_url,region,token_ciphertext,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",(mid,name,provider,name,url,"",encrypt_token(token),ts,ts))
    github_secret(PROVIDER_SECRET[provider],token); github_secret("TRADINGAGENTS_LLM_PROVIDER",provider); github_secret("TRADINGAGENTS_DEEP_THINK_LLM",name); github_secret("TRADINGAGENTS_QUICK_THINK_LLM",name); github_secret("TRADINGAGENTS_LLM_BACKEND_URL",url); github_secret("TRADINGAGENTS_CHECKPOINT_ENABLED","true")
    set_active(mid); wclear(chat)
    edit(chat,w["prompt_message_id"],f"✅ <b>مدل آماده و فعال شد</b>\nمدل: <code>{esc(name)}</code>\nProvider: <code>{esc(provider)}</code>",kb([[("▶️ اجرا","run"),("🧪 تست","tests")],[("🤖 مدل‌ها","models")]]))

def handle_wizard(message):
    chat=int(message["chat"]["id"]); uid=int(message["from"]["id"]); w=wrow(chat)
    if not w: return False
    if w["user_id"]!=uid or not admin(uid): return False
    text=(message.get("text") or "").strip(); mid=w["prompt_message_id"]
    if not text: return True
    try:
        if w["stage"]=="base_url":
            url="" if text=="-" else text
            if url and not url.startswith(("http://","https://")): raise ValueError("Base URL باید با http:// یا https:// شروع شود.")
            p=infer_provider(url) if url else "openai_compatible"; wprompt(chat,uid,"token","<b>2/3 — Token</b>\nتوکن API را بفرست.",url,provider=p,mid=mid); return True
        if w["stage"]=="token":
            if len(text)<3: raise ValueError("Token معتبر نیست.")
            wprompt(chat,uid,"model_name","<b>3/3 — Model name</b>\nنام دقیق مدل را بفرست.",w["base_url"],text,provider=w["provider"],mid=mid); return True
        if w["stage"]=="model_name":
            wsave(chat,uid,"confirm",w["base_url"],w["token"],text,w["provider"],mid)
            buttons=[[ ("⚡ OpenAI-compatible","provider:openai_compatible") ],[("OpenAI","provider:openai"),("Anthropic","provider:anthropic")],[("Google","provider:google"),("DeepSeek","provider:deepseek")],[("NVIDIA","provider:nvidia"),("OpenRouter","provider:openrouter")],[("❌ لغو","wizard_cancel")]]
            edit(chat,mid,f"<b>Provider</b>\nمدل: <code>{esc(text)}</code>\n\nانتخاب کن:",kb(buttons)); return True
    except Exception as e:
        edit(chat,mid,f"❌ {esc(e)}\n\nهمین مرحله را دوباره بفرست.",kb([[ ("❌ لغو","wizard_cancel") ]]))
    return True

# ---------- commands/callbacks ----------
def command(message):
    chat=int(message["chat"]["id"]); uid=int(message["from"]["id"]); text=(message.get("text") or "").strip()
    if not authorized(uid): return
    if text.startswith("/start"):
        flow_clear(chat); wclear(chat); main_menu(chat); return
    if handle_wizard(message): return
    if handle_flow(message): return
    # Backward-compatible commands; the normal UI does not require them.
    if text.startswith("/analyze"):
        parts=text.split()
        if len(parts)<3:
            start_analysis_flow(chat,uid); return
        try:
            rid,mid=dispatch_run("analysis",ticker=parts[1],date=valid_date(parts[2]),analysts=parts[3] if len(parts)>3 else "")
            send(chat,f"✅ تحلیل ارسال شد.\nشناسه: <code>{esc(rid)}</code>")
        except Exception as e: send(chat,f"❌ <code>{esc(e)}</code>")
        return
    if text.startswith("/backtest"):
        parts=text.split()
        if len(parts)<4:
            start_backtest_flow(chat,uid); return
        try:
            every=int(parts[4]) if len(parts)>4 else 7; analysts=parts[5] if len(parts)>5 else ""
            rid,mid=dispatch_run("backtest",tickers=parts[1],start=valid_date(parts[2]),end=valid_date(parts[3]),every=every,analysts=analysts)
            send(chat,f"✅ بک‌تست ارسال شد.\nشناسه: <code>{esc(rid)}</code>")
        except Exception as e: send(chat,f"❌ <code>{esc(e)}</code>")
        return

def callback(q):
    uid=int(q.get("from",{}).get("id",0)); m=q.get("message") or {}; chat=int(m.get("chat",{}).get("id",uid)); mid=int(m.get("message_id",0)); data=q.get("data","")
    if not authorized(uid): answer_cb(q["id"],"دسترسی مجاز نیست.",True); return
    answer_cb(q["id"])
    try:
        if data=="home": main_menu(chat,mid)
        elif data=="models": model_menu(chat,mid)
        elif data=="add":
            if admin(uid): start_wizard(chat,uid,mid)
        elif data=="wizard_cancel": wclear(chat); main_menu(chat,mid)
        elif data.startswith("provider:"):
            w=wrow(chat)
            if w and admin(uid) and w["stage"]=="confirm": finish_model(chat,w,data.split(":",1)[1])
        elif data.startswith("activate:"): activate(data.split(":",1)[1]); model_menu(chat,mid)
        elif data=="run": run_menu(chat,mid)
        elif data=="new_analysis":
            if not active_id(): raise ValueError("اول از بخش مدل‌ها یک مدل را فعال کن.")
            start_analysis_flow(chat,uid,mid)
        elif data=="new_backtest":
            if not active_id(): raise ValueError("اول از بخش مدل‌ها یک مدل را فعال کن.")
            start_backtest_flow(chat,uid,mid)
        elif data=="flow_cancel": flow_clear(chat); main_menu(chat,mid)
        elif data=="flow_analysis_all":
            w=flow_row(chat); d=flow_data(w); d["analysts"]=""; flow_finish_analysis(chat,mid,d)
        elif data=="flow_analysis_pick":
            w=flow_row(chat); d=flow_data(w); flow_save(chat,uid,"analysis","pick_analysts",d,mid)
            edit(chat,mid,"<b>انتخاب تحلیلگران</b>\nمی‌توانی چند مورد را انتخاب کنی؛ بعد «شروع تحلیل» را بزن.",kb([[('📊 Market','pick:market'),('📰 News','pick:news')],[('💬 Sentiment','pick:sentiment'),('💰 Fundamentals','pick:fundamentals')],[('🚀 شروع تحلیل','flow_analysis_start')],[('❌ لغو','flow_cancel')]]))
        elif data.startswith("pick:"):
            w=flow_row(chat); d=flow_data(w); chosen=d.get("analysts",[]); a=data.split(":",1)[1];
            if not isinstance(chosen,list): chosen=[]
            if a in chosen: chosen.remove(a)
            else: chosen.append(a)
            d["analysts"]=chosen; flow_save(chat,uid,"analysis","pick_analysts",d,mid)
            labels={'market':'📊 Market','news':'📰 News','sentiment':'💬 Sentiment','fundamentals':'💰 Fundamentals'}
            rows=[[((('✅ ' if x in chosen else '')+labels[x]),f'pick:{x}') for x in ('market','news')],[((('✅ ' if x in chosen else '')+labels[x]),f'pick:{x}') for x in ('sentiment','fundamentals')],[('🚀 شروع تحلیل','flow_analysis_start')],[('❌ لغو','flow_cancel')]]
            edit(chat,mid,"<b>انتخاب تحلیلگران</b>\nموارد انتخاب‌شده علامت ✅ دارند.",kb(rows))
        elif data=="flow_analysis_start":
            w=flow_row(chat); d=flow_data(w);
            chosen=d.get("analysts",[]); d["analysts"]=','.join(chosen) if isinstance(chosen,list) else str(chosen or '')
            if not d["analysts"]: raise ValueError("حداقل یک تحلیلگر انتخاب کن یا «همه تحلیلگران» را بزن.")
            flow_finish_analysis(chat,mid,d)
        elif data.startswith("flow_every:"):
            w=flow_row(chat); d=flow_data(w); d["every"]=int(data.split(":",1)[1]); d["analysts"]="";
            flow_finish_backtest(chat,mid,d)
        elif data=="sample":
            rid,mm=dispatch_run("analysis",ticker="AAPL",date=dt.date.today().isoformat(),analysts="market")
            edit(chat,mid,f"✅ اجرای نمونه ارسال شد.\nRequest: <code>{rid}</code>",kb([[("📊 وضعیت","status"),("◀️ خانه","home")]]))
        elif data=="status": menu(chat,status_text(),[[("🔄 تازه‌سازی","status"),("◀️ خانه","home")]],mid)
        elif data=="history": history_menu(chat,mid)
        elif data=="artifacts": artifacts_menu(chat,mid)
        elif data=="tests": test_menu(chat,mid)
        elif data.startswith("test:"):
            activate(data.split(":",1)[1]); rid,mm=dispatch_run("analysis",ticker="AAPL",date=dt.date.today().isoformat(),analysts="market"); send(chat,f"🧪 تست ارسال شد.\nRequest: <code>{rid}</code>\nمدل: <code>{esc(mm)}</code>")
        elif data=="delmenu": delete_menu(chat,mid)
        elif data.startswith("delete:"):
            if not admin(uid): return
            model_id=data.split(":",1)[1]; db("UPDATE models SET enabled=0,updated_at=? WHERE id=?",(now(),model_id))
            if active_id()==model_id: db("DELETE FROM settings WHERE key='active_model'")
            model_menu(chat,mid)
    except Exception as e:
        try: edit(chat,mid,f"❌ {esc(e)}",kb([[ ("◀️ خانه","home") ]]))
        except Exception: send(chat,f"❌ <code>{esc(e)}</code>")

def poll():
    if not BOT_TOKEN: raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not GH_TOKEN: raise SystemExit("BOT_GITHUB_TOKEN is required")
    if not ADMIN_IDS and not ALLOWED_IDS: raise SystemExit("TELEGRAM_ADMIN_IDS or TELEGRAM_ALLOWED_USER_IDS is required")
    offset=0; me=tg("getMe"); print(f"TradingAgents bot started @{me.get('username','')}",flush=True)
    while True:
        try:
            updates=tg("getUpdates",{"offset":offset,"timeout":50,"allowed_updates":["message","callback_query"]}) or []
            for u in updates:
                offset=int(u["update_id"])+1
                try:
                    if "callback_query" in u: callback(u["callback_query"])
                    elif "message" in u: command(u["message"])
                except Exception: traceback.print_exc()
        except Exception as e:
            print(f"Polling error: {e}",flush=True); time.sleep(5)

if __name__=="__main__": poll()
