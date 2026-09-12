import os, re, json, time, base64, sqlite3, secrets, hashlib, hmac, asyncio, struct, math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from google import genai
from google.genai import types

APP_NAME=os.getenv("APP_NAME","Maneshi AI")
DB_PATH=os.getenv("DATABASE_PATH","/data/maneshi.db")
DATA_DIR=Path(os.getenv("DATA_DIR","/data"))
DATA_DIR.mkdir(parents=True,exist_ok=True)
UPLOAD_DIR=DATA_DIR/"uploads"; BACKUP_DIR=DATA_DIR/"backups"
UPLOAD_DIR.mkdir(parents=True,exist_ok=True); BACKUP_DIR.mkdir(parents=True,exist_ok=True)

# Railway-friendly persistent runtime secrets.
# If explicit env vars are not supplied, secrets are generated once and stored on /data.
def _runtime_secrets():
    path=DATA_DIR/".maneshi-runtime-secrets.json"
    saved={}
    try:
        if path.exists(): saved=json.loads(path.read_text())
    except Exception:
        saved={}
    changed=False
    def get(name, factory):
        nonlocal changed
        value=os.getenv(name,"").strip() or saved.get(name,"")
        if not value:
            value=factory(); saved[name]=value; changed=True
        return value
    vals={
      "APP_SECRET": get("APP_SECRET", lambda: secrets.token_hex(32)),
      "MASTER_KEY": get("MASTER_KEY", lambda: base64.urlsafe_b64encode(os.urandom(32)).decode()),
      "TELEPHONY_SECRET": get("TELEPHONY_SECRET", lambda: secrets.token_hex(32)),
      "PAYMENT_WEBHOOK_SECRET": get("PAYMENT_WEBHOOK_SECRET", lambda: secrets.token_hex(32)),
    }
    if changed:
        try:
            path.write_text(json.dumps(saved,ensure_ascii=False,indent=2)); os.chmod(path,0o600)
        except Exception:
            pass
    return vals

_RUNTIME=_runtime_secrets()
APP_SECRET=_RUNTIME["APP_SECRET"]
FERNET=Fernet(_RUNTIME["MASTER_KEY"].encode())
DEFAULT_LICENSE=os.getenv("DEFAULT_LICENSE_KEY","MANSHI-DEMO-2026")
LICENSE_SERVER_URL=os.getenv("LICENSE_SERVER_URL","").strip()
TELEPHONY_SECRET=_RUNTIME["TELEPHONY_SECRET"]
PAYMENT_WEBHOOK_SECRET=_RUNTIME["PAYMENT_WEBHOOK_SECRET"]
DEFAULT_TEXT_MODEL=os.getenv("GEMINI_TEXT_MODEL","gemini-2.5-flash")
DEFAULT_LIVE_MODEL=os.getenv("GEMINI_LIVE_MODEL","gemini-3.1-flash-live-preview")
DEFAULT_VOICE=os.getenv("GEMINI_VOICE","Kore")
DOMAIN=(os.getenv("DOMAIN","").strip() or os.getenv("RAILWAY_PUBLIC_DOMAIN","").strip())

app=FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, same_site="lax", https_only=bool(DOMAIN))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates=Jinja2Templates(directory="templates")

DB_LOCK=asyncio.Lock()

def db():
    c=sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c=db(); cur=c.cursor()
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,phone TEXT UNIQUE,vip INTEGER DEFAULT 0,hot_score INTEGER DEFAULT 0,notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY AUTOINCREMENT,caller TEXT,direction TEXT DEFAULT 'inbound',started_at TEXT DEFAULT CURRENT_TIMESTAMP,duration INTEGER DEFAULT 0,status TEXT DEFAULT 'completed',summary TEXT DEFAULT '',transcript TEXT DEFAULT '',recording_url TEXT DEFAULT '',sentiment TEXT DEFAULT '',lead_score INTEGER DEFAULT 0,important INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,phone TEXT,starts_at TEXT,status TEXT DEFAULT 'confirmed',service TEXT DEFAULT '',notes TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,phone TEXT,amount REAL DEFAULT 0,reference TEXT DEFAULT '',receipt_date TEXT DEFAULT '',status TEXT DEFAULT 'pending',image_path TEXT DEFAULT '',raw_json TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,verified_at TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,phone TEXT,total REAL DEFAULT 0,status TEXT DEFAULT 'new',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,type TEXT DEFAULT 'service',price REAL DEFAULT 0,stock INTEGER DEFAULT 0,active INTEGER DEFAULT 1,description TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS knowledge(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,content TEXT,category TEXT DEFAULT 'general',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,phone TEXT,channel TEXT DEFAULT 'sms',content TEXT,direction TEXT DEFAULT 'outbound',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,phone TEXT,role TEXT DEFAULT 'operator',active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS holidays(id INTEGER PRIMARY KEY AUTOINCREMENT,date TEXT UNIQUE,title TEXT DEFAULT 'تعطیل',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_id INTEGER,phone TEXT,due_at TEXT,status TEXT DEFAULT 'pending',note TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS branches(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,address TEXT DEFAULT '',phone TEXT DEFAULT '',active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS agents(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,role TEXT DEFAULT 'پذیرش',voice TEXT DEFAULT 'Kore',tone TEXT DEFAULT 'محترمانه و صمیمی',active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,details TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    ''')
    defaults={
      "license_active":"0","license_key":"","business_name":"کسب‌وکار من","business_description":"",
      "ai_enabled":"1","answer_mode":"no_answer","no_answer_seconds":"20","business_status":"open",
      "working_hours":"شنبه تا پنجشنبه 09:00 تا 20:00","ai_tone":"محترمانه، طبیعی، کوتاه و حرفه‌ای",
      "ai_rules":"هرگز اطلاعاتی که در دانش کسب‌وکار نیست جعل نکن. برای موارد حساس یا شکایت جدی تماس را به مدیر ارجاع بده.",
      "gemini_api_key":"","gemini_text_model":DEFAULT_TEXT_MODEL,"gemini_live_model":DEFAULT_LIVE_MODEL,"gemini_voice":DEFAULT_VOICE,
      "twilio_account_sid":"","twilio_auth_token":"","twilio_number":"","manager_number":"",
      "sip_host":"","sip_username":"","sip_password":"","payment_provider":"generic","daily_brief":"1",
      "auto_followup":"0","record_calls":"0","recording_consent_message":"این تماس ممکن است برای بهبود کیفیت ضبط شود.",
      "price_currency":"تومان","timezone":os.getenv("TZ","Asia/Tehran"),"active_agent":"سارا"
    }
    for k,v in defaults.items(): cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",(k,v))
    if not cur.execute("SELECT 1 FROM agents LIMIT 1").fetchone():
        cur.execute("INSERT INTO agents(name,role,voice,tone) VALUES (?,?,?,?)",("سارا","پذیرش",DEFAULT_VOICE,"محترمانه و صمیمی"))
    c.commit(); c.close()

init_db()

def setting(key, default=""):
    c=db(); r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone(); c.close(); return r[0] if r else default

def set_setting(key,value):
    c=db(); c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value))); c.commit(); c.close()

def enc(v:str)->str:
    return FERNET.encrypt(v.encode()).decode() if v else ""
def dec(v:str)->str:
    if not v: return ""
    try: return FERNET.decrypt(v.encode()).decode()
    except Exception: return ""

def audit(actor,action,details=""):
    c=db(); c.execute("INSERT INTO audit_log(actor,action,details) VALUES (?,?,?)",(actor,action,details[:4000])); c.commit(); c.close()

def password_ok(password):
    raw=os.getenv("ADMIN_PASSWORD_HASH","").strip()
    if raw:
        try:
            salt_hex,hash_hex=raw.split("$",1); salt=bytes.fromhex(salt_hex)
            cand=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32).hex()
            return hmac.compare_digest(cand,hash_hex)
        except Exception:
            return False
    # Railway test mode: keep ADMIN_PASSWORD as a sealed/secret service variable.
    expected=os.getenv("ADMIN_PASSWORD","Admin-ChangeMe-2026!")
    return hmac.compare_digest(password,expected)

def is_auth(req:Request): return bool(req.session.get("auth")) and setting("license_active")=="1"
def need_auth(req:Request):
    if not is_auth(req): raise HTTPException(401,"نیاز به ورود")

def public_url(req:Request):
    if DOMAIN: return "https://"+DOMAIN
    return str(req.base_url).rstrip("/")

def safe_json(text):
    text=(text or "").strip()
    text=re.sub(r"^```(?:json)?\s*|\s*```$","",text,flags=re.I|re.S)
    m=re.search(r"(\{.*\}|\[.*\])",text,re.S)
    if m: text=m.group(1)
    return json.loads(text)

def gemini_client():
    key=dec(setting("gemini_api_key"))
    if not key: raise RuntimeError("Gemini API Key تنظیم نشده است")
    return genai.Client(api_key=key)

def business_context():
    c=db()
    prods=[dict(r) for r in c.execute("SELECT name,type,price,stock,active,description FROM products WHERE active=1 ORDER BY id DESC LIMIT 100")]
    know=[dict(r) for r in c.execute("SELECT title,content,category FROM knowledge ORDER BY id DESC LIMIT 100")]
    c.close()
    return {
      "business_name":setting("business_name"),"description":setting("business_description"),
      "status":setting("business_status"),"hours":setting("working_hours"),"tone":setting("ai_tone"),
      "rules":setting("ai_rules"),"currency":setting("price_currency"),"products":prods,"knowledge":know
    }

def system_instruction(caller=""):
    ctx=business_context()
    consent=setting("recording_consent_message") if setting("record_calls")=="1" else ""
    consent_rule=("در اولین جمله، قبل از ادامه مکالمه، این اطلاع را کوتاه و واضح بگو: "+consent) if consent else ""
    return f'''تو «{setting("active_agent","سارا")}»، منشی هوشمند فارسی برای {ctx["business_name"]} هستی.
{consent_rule}
فقط فارسی طبیعی و روان صحبت کن مگر مشتری زبان دیگری را صریحاً انتخاب کند. پاسخ‌ها کوتاه، مؤدب و کاربردی باشند.
وضعیت کسب‌وکار: {ctx["status"]}. ساعات کاری: {ctx["hours"]}.
لحن: {ctx["tone"]}
قوانین قطعی: {ctx["rules"]}
اطلاعات کسب‌وکار: {ctx["description"]}
محصولات/خدمات: {json.dumps(ctx["products"],ensure_ascii=False)}
دانش تأییدشده: {json.dumps(ctx["knowledge"],ensure_ascii=False)}
شماره تماس‌گیرنده: {caller or 'نامشخص'}
هرگز پرداخت را فقط از روی تصویر رسید قطعی اعلام نکن؛ تنها اگر وضعیت سیستم verified بود آن را تأیید کن.
اگر مشتری نوبت خواست از ابزار create_appointment استفاده کن. اگر نیاز به پیگیری بود create_followup و اگر موضوع بحرانی/شکایت شدید بود mark_important_call را صدا بزن.
اگر جواب را نمی‌دانی صادقانه بگو برای بررسی به مدیر منتقل/ثبت می‌کنی.'''

async def verify_license_remote(key, instance_id):
    if not LICENSE_SERVER_URL: return None
    try:
      async with httpx.AsyncClient(timeout=8) as client:
        r=await client.post(LICENSE_SERVER_URL.rstrip("/")+"/verify",json={"license_key":key,"instance_id":instance_id})
        if r.status_code==200: return r.json()
    except Exception: pass
    return {"valid":False,"reason":"license_server_unreachable"}

@app.get("/health")
def health(): return {"ok":True,"time":datetime.now(timezone.utc).isoformat()}

@app.get("/",response_class=HTMLResponse)
def landing(request:Request):
    if is_auth(request): return RedirectResponse("/panel",302)
    return templates.TemplateResponse("landing.html",{"request":request,"app_name":APP_NAME})

@app.get("/login",response_class=HTMLResponse)
def login_page(request:Request):
    return templates.TemplateResponse("login.html",{"request":request,"license_active":setting("license_active")=="1","app_name":APP_NAME})

@app.post("/api/license/activate")
async def activate_license(request:Request):
    body=await request.json(); key=(body.get("key") or "").strip()
    if not key: return JSONResponse({"ok":False,"message":"لایسنس را وارد کنید"},400)
    instance=hashlib.sha256((os.uname().nodename+os.getenv("APP_SECRET","")).encode()).hexdigest()[:24]
    remote=await verify_license_remote(key,instance)
    valid = bool(remote and remote.get("valid")) if remote is not None else hmac.compare_digest(key,DEFAULT_LICENSE)
    if not valid: return JSONResponse({"ok":False,"message":"لایسنس معتبر نیست"},403)
    set_setting("license_active","1"); set_setting("license_key",enc(key)); set_setting("instance_id",instance)
    if remote: set_setting("license_plan",remote.get("plan","licensed")); set_setting("license_expires",remote.get("expires_at",""))
    audit("system","license_activated",instance)
    return {"ok":True,"message":"لایسنس فعال شد"}

@app.post("/api/login")
async def login(request:Request):
    if setting("license_active")!="1": return JSONResponse({"ok":False,"message":"ابتدا لایسنس را فعال کنید"},403)
    body=await request.json(); user=(body.get("username") or "").strip(); pwd=body.get("password") or ""
    if hmac.compare_digest(user,os.getenv("ADMIN_USER","admin")) and password_ok(pwd):
      request.session["auth"]=True; request.session["user"]=user; audit(user,"login"); return {"ok":True}
    return JSONResponse({"ok":False,"message":"نام کاربری یا رمز اشتباه است"},401)

@app.post("/api/logout")
def logout(request:Request): request.session.clear(); return {"ok":True}

@app.get("/panel",response_class=HTMLResponse)
def panel(request:Request):
    if not is_auth(request): return RedirectResponse("/login",302)
    return templates.TemplateResponse("panel.html",{"request":request,"app_name":APP_NAME,"user":request.session.get("user","admin")})

@app.get("/api/bootstrap")
def bootstrap(request:Request):
    need_auth(request)
    c=db()
    stats={
      "calls_today":c.execute("SELECT COUNT(*) FROM calls WHERE date(started_at)=date('now','localtime')").fetchone()[0],
      "ai_calls_today":c.execute("SELECT COUNT(*) FROM calls WHERE date(started_at)=date('now','localtime') AND status='ai' ").fetchone()[0],
      "appointments_today":c.execute("SELECT COUNT(*) FROM appointments WHERE date(starts_at)=date('now','localtime') AND status!='cancelled'").fetchone()[0],
      "verified_payments":c.execute("SELECT COUNT(*) FROM receipts WHERE status='verified'").fetchone()[0],
      "pending_receipts":c.execute("SELECT COUNT(*) FROM receipts WHERE status IN ('pending','suspicious')").fetchone()[0],
      "customers":c.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
      "hot_leads":c.execute("SELECT COUNT(*) FROM customers WHERE hot_score>=70").fetchone()[0],
      "pending_followups":c.execute("SELECT COUNT(*) FROM followups WHERE status='pending'").fetchone()[0]
    }
    recent_calls=[dict(r) for r in c.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 8")]
    upcoming=[dict(r) for r in c.execute("SELECT * FROM appointments WHERE status!='cancelled' ORDER BY starts_at ASC LIMIT 8")]
    c.close()
    cfg={k:setting(k) for k in ["business_name","business_status","ai_enabled","answer_mode","no_answer_seconds","working_hours","active_agent","gemini_voice","gemini_live_model","price_currency","record_calls"]}
    cfg["gemini_configured"]=bool(dec(setting("gemini_api_key")))
    cfg["twilio_configured"]=bool(dec(setting("twilio_auth_token")) and setting("twilio_account_sid") and setting("twilio_number"))
    return {"stats":stats,"recent_calls":recent_calls,"upcoming":upcoming,"config":cfg,"public_url":public_url(request)}

ALLOWED_LIST={"customers","calls","appointments","receipts","orders","products","knowledge","messages","staff","holidays","followups","branches","agents","audit_log"}
@app.get("/api/list/{kind}")
def list_rows(kind:str,request:Request,q:str="",limit:int=100):
    need_auth(request)
    if kind not in ALLOWED_LIST: raise HTTPException(404)
    limit=max(1,min(limit,500)); c=db()
    if q:
      cols=[r[1] for r in c.execute(f"PRAGMA table_info({kind})") if str(r[2]).upper().startswith("TEXT")]
      if cols:
        where=" OR ".join([f"{x} LIKE ?" for x in cols]); params=[f"%{q}%"]*len(cols)+[limit]
        rows=[dict(r) for r in c.execute(f"SELECT * FROM {kind} WHERE {where} ORDER BY id DESC LIMIT ?",params)]
      else: rows=[]
    else: rows=[dict(r) for r in c.execute(f"SELECT * FROM {kind} ORDER BY id DESC LIMIT ?",(limit,))]
    c.close(); return {"items":rows}

CREATE_FIELDS={
 "customers":["name","phone","vip","hot_score","notes"],"appointments":["customer_name","phone","starts_at","status","service","notes"],
 "orders":["customer_name","phone","total","status"],"products":["name","type","price","stock","active","description"],
 "knowledge":["title","content","category"],"messages":["customer_name","phone","channel","content","direction"],
 "staff":["name","phone","role","active"],"holidays":["date","title"],"followups":["customer_id","phone","due_at","status","note"],
 "branches":["name","address","phone","active"],"agents":["name","role","voice","tone","active"]
}
@app.post("/api/create/{kind}")
async def create_row(kind:str,request:Request):
    need_auth(request)
    if kind not in CREATE_FIELDS: raise HTTPException(404)
    body=await request.json(); fields=[x for x in CREATE_FIELDS[kind] if x in body]
    if not fields: return JSONResponse({"ok":False,"message":"اطلاعات کافی نیست"},400)
    vals=[body[x] for x in fields]; c=db()
    try:
      cur=c.execute(f"INSERT INTO {kind}({','.join(fields)}) VALUES ({','.join(['?']*len(fields))})",vals); c.commit(); rid=cur.lastrowid
    except sqlite3.IntegrityError as e: c.close(); return JSONResponse({"ok":False,"message":str(e)},400)
    c.close(); audit(request.session.get("user","admin"),"create_"+kind,json.dumps(body,ensure_ascii=False)); return {"ok":True,"id":rid}

@app.post("/api/update/{kind}/{rid}")
async def update_row(kind:str,rid:int,request:Request):
    need_auth(request)
    allowed=set(CREATE_FIELDS.get(kind,[]))|({"status","verified_at","important","lead_score","summary","transcript"} if kind in {"receipts","calls"} else set())
    if not allowed: raise HTTPException(404)
    body=await request.json(); fields=[x for x in body if x in allowed]
    if not fields: return {"ok":True}
    vals=[body[x] for x in fields]+[rid]; c=db(); c.execute(f"UPDATE {kind} SET "+",".join([f"{x}=?" for x in fields])+" WHERE id=?",vals); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/delete/{kind}/{rid}")
def delete_row(kind:str,rid:int,request:Request):
    need_auth(request)
    if kind not in CREATE_FIELDS: raise HTTPException(404)
    c=db(); c.execute(f"DELETE FROM {kind} WHERE id=?",(rid,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/settings")
def get_settings(request:Request):
    need_auth(request); c=db(); rows=c.execute("SELECT key,value FROM settings").fetchall(); c.close()
    hidden={"gemini_api_key","twilio_auth_token","sip_password","license_key"}
    out={r[0]:("••••••••" if r[0] in hidden and r[1] else r[1]) for r in rows}
    out["gemini_configured"]=bool(dec(setting("gemini_api_key"))); out["twilio_auth_configured"]=bool(dec(setting("twilio_auth_token")))
    return out

SECRET_SETTINGS={"gemini_api_key","twilio_auth_token","sip_password"}
SAFE_SETTINGS={"business_name","business_description","ai_enabled","answer_mode","no_answer_seconds","business_status","working_hours","ai_tone","ai_rules","gemini_text_model","gemini_live_model","gemini_voice","twilio_account_sid","twilio_number","manager_number","sip_host","sip_username","payment_provider","daily_brief","auto_followup","record_calls","recording_consent_message","price_currency","timezone","active_agent"}|SECRET_SETTINGS
@app.post("/api/settings")
async def save_settings(request:Request):
    need_auth(request); body=await request.json()
    for k,v in body.items():
      if k not in SAFE_SETTINGS: continue
      if k in SECRET_SETTINGS:
        if v and v!="••••••••": set_setting(k,enc(str(v)))
      else: set_setting(k,str(v))
    audit(request.session.get("user","admin"),"settings_update",",".join(body.keys())); return {"ok":True}

async def ai_text(prompt, system=""):
    client=gemini_client(); model=setting("gemini_text_model",DEFAULT_TEXT_MODEL)
    def run():
      return client.models.generate_content(model=model,contents=prompt,config={"system_instruction":system} if system else None)
    r=await asyncio.to_thread(run); return (r.text or "").strip()

@app.post("/api/ai/test")
async def ai_test(request:Request):
    need_auth(request); body=await request.json()
    try: text=await ai_text(body.get("message","سلام"),system_instruction("تست پنل")); return {"ok":True,"reply":text}
    except Exception as e: return JSONResponse({"ok":False,"message":str(e)},400)

@app.post("/api/ai/command")
async def ai_command(request:Request):
    need_auth(request); body=await request.json(); command=(body.get("command") or "").strip()
    if not command: return JSONResponse({"ok":False,"message":"دستور خالی است"},400)
    parser='''تو موتور کنترل پنل منشی فارسی هستی. دستور مدیر را فقط به JSON تبدیل کن. هیچ متن دیگری ننویس.
فرمت: {"intent":"...","args":{},"confirmation":"متن کوتاه فارسی"}
intentهای مجاز:
set_ai_enabled(enabled), set_answer_mode(mode:always|no_answer|after_hours,seconds), set_business_status(status), add_holiday(date,title), set_hours(hours), set_tone(tone), set_rule(rule), add_vip(name,phone), add_knowledge(title,content,category), update_product_price(name,price), search(query), report(period), none.
اگر تاریخ نسبی مثل فردا بود با توجه به تاریخ امروز آن را YYYY-MM-DD کن. اگر عملی خارج از این لیست بود none.'''
    try:
      today=datetime.now().strftime("%Y-%m-%d")
      raw=await ai_text(f"تاریخ امروز: {today}\nدستور مدیر: {command}",parser); obj=safe_json(raw)
      intent=obj.get("intent","none"); a=obj.get("args") or {}
      if intent=="set_ai_enabled": set_setting("ai_enabled","1" if a.get("enabled") else "0")
      elif intent=="set_answer_mode":
        mode=a.get("mode","no_answer"); set_setting("answer_mode",mode)
        if a.get("seconds"): set_setting("no_answer_seconds",str(max(5,min(60,int(a["seconds"])))))
      elif intent=="set_business_status": set_setting("business_status",a.get("status","open"))
      elif intent=="add_holiday":
        c=db(); c.execute("INSERT OR IGNORE INTO holidays(date,title) VALUES (?,?)",(a.get("date"),a.get("title","تعطیل"))); c.commit(); c.close()
      elif intent=="set_hours": set_setting("working_hours",a.get("hours",""))
      elif intent=="set_tone": set_setting("ai_tone",a.get("tone",""))
      elif intent=="set_rule": set_setting("ai_rules",(setting("ai_rules")+"\n"+a.get("rule","")).strip())
      elif intent=="add_vip":
        c=db(); c.execute("INSERT INTO customers(name,phone,vip) VALUES (?,?,1) ON CONFLICT(phone) DO UPDATE SET name=excluded.name,vip=1",(a.get("name","VIP"),a.get("phone",""))); c.commit(); c.close()
      elif intent=="add_knowledge":
        c=db(); c.execute("INSERT INTO knowledge(title,content,category) VALUES (?,?,?)",(a.get("title","یادداشت"),a.get("content",""),a.get("category","general"))); c.commit(); c.close()
      elif intent=="update_product_price":
        c=db(); c.execute("UPDATE products SET price=? WHERE name LIKE ?",(float(a.get("price",0)),"%"+a.get("name","")+"%")); c.commit(); c.close()
      audit(request.session.get("user","admin"),"ai_command",json.dumps(obj,ensure_ascii=False))
      return {"ok":True,"intent":intent,"args":a,"message":obj.get("confirmation") or "انجام شد."}
    except Exception as e: return JSONResponse({"ok":False,"message":"اجرای دستور ناموفق بود: "+str(e)},400)

@app.post("/api/receipts/upload")
async def receipt_upload(request:Request,file:UploadFile=File(...),customer_name:str=Form(""),phone:str=Form("")):
    need_auth(request)
    data=await file.read();
    if len(data)>8*1024*1024: return JSONResponse({"ok":False,"message":"حداکثر حجم ۸ مگابایت است"},400)
    suffix=Path(file.filename or "receipt.jpg").suffix.lower()[:8] or ".jpg"; name=secrets.token_hex(12)+suffix; path=UPLOAD_DIR/name; path.write_bytes(data)
    extracted={"amount":0,"reference":"","date":"","suspicious":False,"notes":""}
    if dec(setting("gemini_api_key")):
      try:
        client=gemini_client(); model=setting("gemini_text_model",DEFAULT_TEXT_MODEL); mime=file.content_type or "image/jpeg"
        prompt='''این تصویر رسید پرداخت است. فقط JSON برگردان: {"amount":number,"reference":"string","date":"string","suspicious":boolean,"notes":"string"}. اگر چیزی خوانا نیست خالی بگذار. هرگز صرفاً از تصویر نتیجه قطعی پرداخت نده.'''
        def run(): return client.models.generate_content(model=model,contents=[prompt,types.Part.from_bytes(data=data,mime_type=mime)])
        resp=await asyncio.to_thread(run); extracted=safe_json(resp.text)
      except Exception as e: extracted["notes"]="OCR/AI error: "+str(e)
    status="suspicious" if extracted.get("suspicious") else "pending"
    c=db(); cur=c.execute("INSERT INTO receipts(customer_name,phone,amount,reference,receipt_date,status,image_path,raw_json) VALUES (?,?,?,?,?,?,?,?)",(customer_name,phone,float(extracted.get("amount") or 0),str(extracted.get("reference") or ""),str(extracted.get("date") or ""),status,str(path),json.dumps(extracted,ensure_ascii=False))); c.commit(); rid=cur.lastrowid; c.close()
    return {"ok":True,"id":rid,"status":status,"extracted":extracted,"message":"رسید خوانده شد؛ برای تأیید قطعی باید با تراکنش واقعی تطبیق داده شود."}

@app.post("/api/payments/webhook")
async def payment_webhook(request:Request,secret:str=""):
    if not hmac.compare_digest(secret,PAYMENT_WEBHOOK_SECRET): raise HTTPException(403)
    body=await request.json(); ref=str(body.get("reference") or ""); amount=float(body.get("amount") or 0); paid=bool(body.get("paid",True))
    if not ref: return JSONResponse({"ok":False},400)
    c=db(); row=c.execute("SELECT * FROM receipts WHERE reference=? ORDER BY id DESC LIMIT 1",(ref,)).fetchone()
    if not row: c.close(); return JSONResponse({"ok":False,"message":"receipt_not_found"},404)
    status="verified" if paid and (not amount or abs(float(row["amount"])-amount)<0.01) else "suspicious"
    c.execute("UPDATE receipts SET status=?,verified_at=? WHERE id=?",(status,datetime.now().isoformat() if status=="verified" else "",row["id"])); c.commit(); c.close(); return {"ok":True,"status":status}

@app.get("/telephony/twilio/incoming",response_class=PlainTextResponse)
def twilio_incoming_get(request:Request):
    return twilio_twiml(request, request.query_params.get("From", ""))

@app.post("/telephony/twilio/incoming",response_class=PlainTextResponse)
async def twilio_incoming_post(request:Request):
    form=await request.form()
    return twilio_twiml(request, str(form.get("From", "")))

def twilio_twiml(request:Request, caller=""):
    base=public_url(request).replace("http://","ws://").replace("https://","wss://")
    ws=f"{base}/telephony/twilio/media"
    xml=f'''<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url="{ws}"><Parameter name="AuthToken" value="{html_escape(TELEPHONY_SECRET)}"/><Parameter name="From" value="{html_escape(caller)}"/></Stream></Connect></Response>'''
    return PlainTextResponse(xml,media_type="application/xml")

def html_escape(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")

def ulaw_decode_byte(u):
    u=(~u)&0xFF; sign=u&0x80; exponent=(u>>4)&0x07; mantissa=u&0x0F
    sample=((mantissa<<3)+0x84)<<exponent; sample-=0x84
    return -sample if sign else sample

def ulaw_encode_sample(sample):
    BIAS=0x84; CLIP=32635; sign=0
    s=int(sample)
    if s<0: sign=0x80; s=-s
    s=min(s,CLIP)+BIAS; exponent=7; mask=0x4000
    while exponent>0 and not (s&mask): exponent-=1; mask>>=1
    mantissa=(s>>(exponent+3))&0x0F
    return (~(sign|(exponent<<4)|mantissa))&0xFF

def mulaw8_to_pcm16k(data:bytes)->bytes:
    samples=[ulaw_decode_byte(b) for b in data]
    up=[]
    for s in samples: up.extend((s,s))
    return struct.pack('<'+'h'*len(up),*up)

def pcm24_to_mulaw8(data:bytes)->bytes:
    n=len(data)//2
    if n<=0: return b''
    samples=struct.unpack('<'+'h'*n,data[:n*2]); down=samples[::3]
    return bytes(ulaw_encode_sample(s) for s in down)

LIVE_TOOLS=[{"function_declarations":[
 {"name":"create_appointment","description":"ثبت نوبت مشتری","parameters":{"type":"OBJECT","properties":{"customer_name":{"type":"STRING"},"phone":{"type":"STRING"},"starts_at":{"type":"STRING"},"service":{"type":"STRING"},"notes":{"type":"STRING"}},"required":["customer_name","starts_at"]}},
 {"name":"create_followup","description":"ثبت پیگیری مشتری","parameters":{"type":"OBJECT","properties":{"phone":{"type":"STRING"},"due_at":{"type":"STRING"},"note":{"type":"STRING"}}}},
 {"name":"mark_important_call","description":"علامت‌گذاری تماس مهم برای مدیر","parameters":{"type":"OBJECT","properties":{"reason":{"type":"STRING"}}}},
 {"name":"lookup_payment","description":"بررسی وضعیت پرداخت با شماره پیگیری","parameters":{"type":"OBJECT","properties":{"reference":{"type":"STRING"}},"required":["reference"]}}
]}]

def execute_live_tool(name,args,caller,call_id):
    c=db()
    try:
      if name=="create_appointment":
        cur=c.execute("INSERT INTO appointments(customer_name,phone,starts_at,service,notes) VALUES (?,?,?,?,?)",(args.get("customer_name","مشتری"),args.get("phone") or caller,args.get("starts_at",""),args.get("service",""),args.get("notes",""))); c.commit(); return {"ok":True,"appointment_id":cur.lastrowid}
      if name=="create_followup":
        cur=c.execute("INSERT INTO followups(phone,due_at,note) VALUES (?,?,?)",(args.get("phone") or caller,args.get("due_at",""),args.get("note",""))); c.commit(); return {"ok":True,"followup_id":cur.lastrowid}
      if name=="mark_important_call":
        c.execute("UPDATE calls SET important=1,summary=CASE WHEN summary='' THEN ? ELSE summary||' | '||? END WHERE id=?",(args.get("reason","مهم"),args.get("reason","مهم"),call_id)); c.commit(); return {"ok":True}
      if name=="lookup_payment":
        r=c.execute("SELECT status,amount,reference FROM receipts WHERE reference=? ORDER BY id DESC LIMIT 1",(args.get("reference",""),)).fetchone(); return dict(r) if r else {"status":"not_found"}
      return {"ok":False,"error":"unknown_tool"}
    finally: c.close()

@app.websocket("/telephony/twilio/media")
async def twilio_media(ws:WebSocket):
    await ws.accept()
    caller=""; stream_sid=""; call_id=None; transcript=[]; started=time.time()
    try:
      # Twilio ابتدا connected و سپس start می‌فرستد. توکن و شماره تماس‌گیرنده از Custom Parameters می‌آیند.
      while True:
        first=await ws.receive_json()
        if first.get("event")=="start":
          stream_sid=first.get("start",{}).get("streamSid","")
          params=first.get("start",{}).get("customParameters",{}) or {}
          if not hmac.compare_digest(str(params.get("AuthToken", "")), TELEPHONY_SECRET):
            await ws.close(code=4403); return
          caller=str(params.get("From","") or "")
          break
        if first.get("event")=="stop": return
      api_key=dec(setting("gemini_api_key"))
      if not api_key:
        await ws.close(code=1011); return
      c=db(); cur=c.execute("INSERT INTO calls(caller,status) VALUES (?,'ai')",(caller,)); c.commit(); call_id=cur.lastrowid; c.close()
      client=genai.Client(api_key=api_key); model=setting("gemini_live_model",DEFAULT_LIVE_MODEL); voice=setting("gemini_voice",DEFAULT_VOICE)
      config={"response_modalities":["AUDIO"],"system_instruction":system_instruction(caller),"speech_config":{"voice_config":{"prebuilt_voice_config":{"voice_name":voice}}},"input_audio_transcription":{},"output_audio_transcription":{},"tools":LIVE_TOOLS}
      async with client.aio.live.connect(model=model,config=config) as session:
        await session.send_realtime_input(text="تماس تازه شروع شده است. همین حالا با یک سلام کوتاه و طبیعی و معرفی خودت، مکالمه را آغاز کن.")
        async def from_twilio():
          while True:
            msg=await ws.receive_json(); ev=msg.get("event")
            if ev=="media":
              raw=base64.b64decode(msg.get("media",{}).get("payload","") or b"")
              if raw:
                await session.send_realtime_input(audio=types.Blob(data=mulaw8_to_pcm16k(raw),mime_type="audio/pcm;rate=16000"))
            elif ev=="stop":
              try: await session.send_realtime_input(audio_stream_end=True)
              except Exception: pass
              return
        async def to_twilio():
          while True:
            async for resp in session.receive():
              if resp.data and stream_sid:
                out=pcm24_to_mulaw8(resp.data)
                if out: await ws.send_json({"event":"media","streamSid":stream_sid,"media":{"payload":base64.b64encode(out).decode()}})
              sc=getattr(resp,"server_content",None)
              if sc:
                it=getattr(sc,"input_transcription",None); ot=getattr(sc,"output_transcription",None)
                if it and getattr(it,"text",None): transcript.append("مشتری: "+it.text)
                if ot and getattr(ot,"text",None): transcript.append("منشی: "+ot.text)
                if getattr(sc,"interrupted",False) and stream_sid:
                  await ws.send_json({"event":"clear","streamSid":stream_sid})
              tc=getattr(resp,"tool_call",None)
              if tc:
                responses=[]
                for fc in tc.function_calls:
                  result=execute_live_tool(fc.name,dict(fc.args or {}),caller,call_id)
                  responses.append(types.FunctionResponse(name=fc.name,id=fc.id,response=result))
                await session.send_tool_response(function_responses=responses)
        tasks=[asyncio.create_task(from_twilio()),asyncio.create_task(to_twilio())]
        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in pending: task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)
    except (WebSocketDisconnect,asyncio.CancelledError): pass
    except Exception as e:
      audit("telephony","live_error",str(e))
    finally:
      if call_id:
        text="\n".join(transcript)[-30000:]; duration=int(time.time()-started); summary=""
        if text and dec(setting("gemini_api_key")):
          try: summary=await ai_text("این مکالمه را در حداکثر ۳ جمله فارسی خلاصه کن و نتیجه/اقدام بعدی را بگو:\n"+text,"خلاصه‌ساز تماس")
          except Exception: pass
        c=db(); c.execute("UPDATE calls SET duration=?,transcript=?,summary=? WHERE id=?",(duration,text,summary,call_id)); c.commit(); c.close()

@app.get("/api/telephony/info")
def telephony_info(request:Request):
    need_auth(request); base=public_url(request)
    return {"twilio_voice_webhook":base+"/telephony/twilio/incoming","media_wss":base.replace('http://','ws://').replace('https://','wss://')+"/telephony/twilio/media","forwarding_note":"شماره شخصی باید در اپراتور روی حالت انتقال تماس هنگام بی‌پاسخ به شماره VoIP/Twilio تنظیم شود. زمان ۲۰ ثانیه معمولاً در خود اپراتور تنظیم می‌شود.","requires_https":True}

@app.post("/api/backup")
def backup(request:Request):
    need_auth(request); ts=datetime.now().strftime("%Y%m%d-%H%M%S"); out=BACKUP_DIR/f"maneshi-{ts}.db"
    src=db(); dst=sqlite3.connect(out); src.backup(dst); dst.close(); src.close(); return {"ok":True,"file":out.name}

@app.get("/api/report/daily")
def daily_report(request:Request):
    need_auth(request); c=db(); today=datetime.now().strftime("%Y-%m-%d")
    r={"date":today,"calls":c.execute("SELECT COUNT(*) FROM calls WHERE date(started_at)=?",(today,)).fetchone()[0],"appointments":c.execute("SELECT COUNT(*) FROM appointments WHERE date(created_at)=?",(today,)).fetchone()[0],"verified_payments":c.execute("SELECT COUNT(*) FROM receipts WHERE date(verified_at)=? AND status='verified'",(today,)).fetchone()[0],"new_customers":c.execute("SELECT COUNT(*) FROM customers WHERE date(created_at)=?",(today,)).fetchone()[0]}; c.close(); return r

async def periodic_backup():
    while True:
      await asyncio.sleep(24*3600)
      try:
        ts=datetime.now().strftime("%Y%m%d-%H%M%S"); out=BACKUP_DIR/f"auto-{ts}.db"; src=db(); dst=sqlite3.connect(out); src.backup(dst); dst.close(); src.close()
        files=sorted(BACKUP_DIR.glob("auto-*.db"),key=lambda p:p.stat().st_mtime,reverse=True)
        for p in files[14:]: p.unlink(missing_ok=True)
      except Exception: pass

@app.on_event("startup")
async def startup_tasks(): asyncio.create_task(periodic_backup())
