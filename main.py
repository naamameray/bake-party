from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

ISRAEL_TZ = ZoneInfo('Asia/Jerusalem')
from typing import Optional, List
from collections import defaultdict
import hashlib
import hmac
import secrets
import time as time_module
import os
import uuid
import psycopg2

app = FastAPI(title="Bake & Party API")

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5433"),
    "dbname": os.environ.get("DB_NAME", "bakeshop"),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "adminpassword"),
}

try:
    init_conn = psycopg2.connect(**DB_CONFIG)
    init_cur = init_conn.cursor()
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS product_categories (
            product_id INTEGER REFERENCES Products(id) ON DELETE CASCADE,
            category_id INTEGER REFERENCES Categories(id) ON DELETE CASCADE,
            PRIMARY KEY (product_id, category_id)
        );
    """)
    init_cur.execute("ALTER TABLE Products ADD COLUMN IF NOT EXISTS notes TEXT;")
    init_cur.execute("ALTER TABLE Products ADD COLUMN IF NOT EXISTS is_on_sale BOOLEAN DEFAULT FALSE;")
    init_cur.execute("ALTER TABLE Products ADD COLUMN IF NOT EXISTS sale_price NUMERIC(10,2);")
    init_cur.execute("ALTER TABLE Products ADD COLUMN IF NOT EXISTS sale_label TEXT;")
    # תאריך הוספה, לשורת "חדש בחנות". בלי ברירת מחדל בעת היצירה, כדי שמוצרים קיימים
    # לא יסומנו כולם כחדשים. רק מוצרים שנוספים מעכשיו מקבלים תאריך.
    init_cur.execute("ALTER TABLE Products ADD COLUMN IF NOT EXISTS created_at TIMESTAMP;")
    init_cur.execute("ALTER TABLE Products ALTER COLUMN created_at SET DEFAULT NOW();")

    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS weekly_hours (
            day_of_week   INTEGER PRIMARY KEY,
            day_name      VARCHAR(20) NOT NULL,
            is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
            opening_time  VARCHAR(5) NOT NULL DEFAULT '09:00',
            closing_time  VARCHAR(5) NOT NULL DEFAULT '18:00'
        );
    """)
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS special_days (
            id            SERIAL PRIMARY KEY,
            holiday_date  DATE UNIQUE NOT NULL,
            title         VARCHAR(255) NOT NULL,
            is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
            opening_time  VARCHAR(5) DEFAULT '09:00',
            closing_time  VARCHAR(5) DEFAULT '18:00',
            note          TEXT
        );
    """)
    day_names = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
    for dow, dname in enumerate(day_names):
        init_cur.execute(
            "INSERT INTO weekly_hours (day_of_week, day_name, is_closed, opening_time, closing_time) "
            "VALUES (%s, %s, FALSE, '09:00', '18:00') ON CONFLICT (day_of_week) DO NOTHING;",
            (dow, dname),
        )
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, name VARCHAR(150), email VARCHAR(255) UNIQUE NOT NULL,
            phone VARCHAR(50), address TEXT, password_hash TEXT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'customer', created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    for col in ("phone", "address", "name"):
        init_cur.execute(f"ALTER TABLE users ALTER COLUMN {col} DROP NOT NULL;")

    # --- תפקידים ואבטחת חשבונות: customer | employee | admin (מנהל) ---
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_logins INTEGER NOT NULL DEFAULT 0;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;")
    # חיבורים נשמרים במסד הנתונים (לא בזיכרון), כך שפרסום חדש לא מנתק אף אחד.
    # נשמר רק ה-hash של הטוקן, לא הטוקן עצמו.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);")
    # יומן פעולות: מי עשה מה ומתי. נכתב רק על ידי השרת, ואי אפשר לערוך או למחוק אותו מהאתר.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id          SERIAL PRIMARY KEY,
            actor_id    INTEGER,
            actor_name  VARCHAR(150),
            action      VARCHAR(60) NOT NULL,
            target_type VARCHAR(40),
            target_id   INTEGER,
            details     TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);")

    # --- שעות עבודה ושכר ---
    # hourly_rate על המשתמש הוא התעריף הנוכחי. כל משמרת שומרת עותק של התעריף שהיה בכניסה,
    # כדי ששינוי תעריף בהמשך לא ישנה חישוב של משמרות קודמות. רק מנהל משנה תעריף או משמרת.
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS hourly_rate NUMERIC(8,2);")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS shifts (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            clock_in    TIMESTAMPTZ NOT NULL,
            clock_out   TIMESTAMPTZ,
            hourly_rate NUMERIC(8,2),
            note        TEXT,
            created_by  INTEGER,
            edited_by   INTEGER,
            edited_at   TIMESTAMPTZ,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_shifts_user_in ON shifts(user_id, clock_in DESC);")
    # עובד אחד לא יכול להיות עם שתי משמרות פתוחות במקביל (מוגן גם ברמת המסד)
    init_cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_shift_per_user ON shifts(user_id) WHERE clock_out IS NULL;")

    # --- הגנה על צ'אט ה-AI: חסימה זמנית על הודעות לא-קשורות ברצף + מגבלת שימוש יומית ---
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_offtopic_streak INTEGER NOT NULL DEFAULT 0;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_blocked_until TIMESTAMP;")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage (
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            usage_date    DATE NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        );
    """)

    # --- יומן שיחות עם ה-AI: מה נשאל, מה נענה, ומי שאל (לצפייה במנהל) ---
    # נשמרים גם ניסיונות שנכשלו (עם סיבת הכשל) — כך אפשר לאבחן תקלות בלי לחפור בלוגים.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_chat_log (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            message     TEXT NOT NULL,
            reply       TEXT,
            topic       VARCHAR(50),
            on_topic    BOOLEAN,
            status      VARCHAR(30) NOT NULL DEFAULT 'ok',
            error_text  TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_log_created ON ai_chat_log(created_at DESC);")

    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value TEXT);
    """)
    init_cur.execute("""
        INSERT INTO settings (key, value) VALUES ('opening_time','09:00'),('closing_time','18:00'),
        ('delivery_override','auto') ON CONFLICT (key) DO NOTHING;
    """)

    # --- שאלות ותשובות נפוצות (FAQ) — כולל מנגנון "מתגלה אוטומטית מהצ'אט" ---
    # topic_key: תגית קבועה שה-AI מסווג אליה כל שאלה בצ'אט (ראו _ai_build_context/ai_chat).
    # ברגע שנושא מסוים נשאל FAQ_AUTO_PUBLISH_THRESHOLD פעמים, הוא עולה אוטומטית לאתר.
    # שאלה/תשובה כתובות מראש ידנית (לא טקסט גולמי מהצ'אט) — כדי לשמור על ניסוח מדויק ומבוקר.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS faq_items (
            id          SERIAL PRIMARY KEY,
            topic_key   VARCHAR(50) UNIQUE,
            question    TEXT NOT NULL,
            answer      TEXT NOT NULL,
            hit_count   INTEGER NOT NULL DEFAULT 0,
            published   BOOLEAN NOT NULL DEFAULT FALSE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        );
    """)
    _faq_seed = [
        ("ordering", "איך מזמינים? יש אפשרות להזמין דרך האתר?",
         "כרגע אין באתר מערכת הזמנות עצמאית — האתר מציג לכם מוצרים, מחירים ושעות פתיחה, ואת ההזמנה עצמה סוגרים בשיחת טלפון או הודעת וואטסאפ. אפשר גם להזמין דרך אפליקציית Wolt.",
         True, 1),
        ("delivery", "יש לכם משלוחים? מעל כמה סכום?",
         "כן, יש משלוחים בתיאום טלפוני או בוואטסאפ, החל מהזמנות מעל 200 ₪. המשלוח מחולק אצלכם לאחר שעות הסגירה.",
         True, 2),
        ("hours", "מה שעות הפעילות שלכם?",
         "השעות המעודכנות תמיד מופיעות בתפריט הצדדי באתר (☰ ← \"שעות פעילות\") — כולל הסטטוס המדויק אם אנחנו פתוחים עכשיו, כי לפעמים יש ימים מיוחדים עם שעות שונות.",
         True, 3),
        ("location", "איפה אתם נמצאים? אפשר להגיע ולבחור בעצמי?",
         "אנחנו חנות פיזית בגבעת שמואל, ושמחים לקבל גם לקוחות שמעדיפים להגיע ולבחור בעצמם, וגם כאלה שמעדיפים לקבל עזרה בבחירה דרך הטלפון או הוואטסאפ. הכתובת המדויקת מופיעה בתפריט הצדדי תחת \"צור קשר\".",
         True, 4),
        ("recommendation", "אפשר לקבל המלצה מה צריך לקנות לעוגה/מסיבה?",
         "בהחלט — יש לנו עוזר/ת AI בצ'אט שיכול/ה להמליץ מה מתאים ולאיזו קטגוריה בחנות לפנות (זמין למשתמשים רשומים, מהתפריט הצדדי ← \"שאל/י את ה-AI שלנו\"). אפשר גם פשוט להתקשר או לשלוח הודעה ונשמח לעזור באופן אישי.",
         True, 5),
        ("sugar_sheets", "יש אפשרות לבדוק גודל דף סוכר לפני שמדפיסים?",
         "כלי כזה נמצא כרגע בפיתוח ויתווסף לאתר בקרוב. בינתיים אפשר לפנות אלינו ישירות ונעזור עם המידע הדרוש.",
         True, 6),
        # שלושת אלה עדיין לא מוצגים באתר — יעלו אוטומטית ברגע שישאלו עליהם מספיק פעמים בצ'אט
        ("prices", "איך אפשר לדעת מחירים? יש מחירון?",
         "המחיר של כל מוצר מופיע ישירות על כרטיס המוצר באתר. אם יש שאלה על מוצר ספציפי שלא מצאתם, אפשר להתקשר או לשלוח הודעת וואטסאפ ונבדוק עבורכם.",
         False, 7),
        ("availability", "איך אני יודע/ת אם מוצר מסוים נמצא במלאי?",
         "מוצר שאזל מהמלאי מסומן בבירור באתר ליד המחיר שלו. לפני שמגיעים במיוחד למוצר מסוים, כדאי גם להתקשר או לשלוח הודעה לוודא זמינות בפועל.",
         False, 8),
        ("contact", "איך יוצרים איתכם קשר?",
         "הכי נוח בטלפון או בוואטסאפ — הפרטים המלאים מופיעים בתפריט הצדדי באתר (☰ ← \"צור קשר\").",
         False, 9),
    ]
    for topic_key, question, answer, published, sort_order in _faq_seed:
        init_cur.execute(
            "INSERT INTO faq_items (topic_key, question, answer, published, sort_order) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (topic_key) DO NOTHING;",
            (topic_key, question, answer, published, sort_order),
        )

    admin_email = os.environ.get("ADMIN_EMAIL")
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if admin_email and admin_pass:
        import hashlib as _h
        _salt = os.urandom(16)
        _dk = _h.pbkdf2_hmac("sha256", admin_pass.encode(), _salt, 200_000)
        _hash = f"pbkdf2_sha256$200000${_salt.hex()}${_dk.hex()}"
        admin_name = os.environ.get("ADMIN_NAME", "מנהל")
        init_cur.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES (%s, %s, %s, 'admin') "
            "ON CONFLICT (email) DO UPDATE SET password_hash=EXCLUDED.password_hash, role='admin', active=TRUE;",
            (admin_name, admin_email.lower(), _hash))

    init_conn.commit()
    init_cur.close()
    init_conn.close()
except Exception as e:
    print("שגיאה בעדכון מסד הנתונים:", e)

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "product_images")
os.makedirs(IMAGES_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

PLACEHOLDER = "data:image/svg+xml;utf8," + "%3Csvg xmlns='http://www.w3.org/2000/svg' width='150' height='150'%3E%3Crect width='150' height='150' fill='%23FFD1DC'/%3E%3Ctext x='50%25' y='52%25' font-size='48' text-anchor='middle' dominant-baseline='middle' fill='%23E05276'%3E🧁%3C/text%3E%3C/svg%3E"

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

SESSION_TTL = 60 * 60 * 24 * 7
ROLES = ("customer", "employee", "admin")
STAFF_ROLES = ("employee", "admin")

def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def _session_from_header(authorization):
    """מחזיר את פרטי המחובר, או None. התפקיד והסטטוס נקראים מהמסד בכל בקשה,
    ולכן שינוי תפקיד או השבתה של משתמש נכנסים לתוקף מיד."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT s.user_id, u.role, u.name, u.must_change_password
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > NOW() AND u.active = TRUE;
            """,
            (_token_hash(token),),
        )
        row = cur.fetchone()
    finally:
        cur.close(); conn.close()
    if not row:
        return None
    return {"user_id": row[0], "role": row[1], "name": row[2], "must_change": bool(row[3]),
            "token_hash": _token_hash(token)}

def require_auth_any(authorization: str = Header(None)):
    """מחובר, גם אם עדיין צריך להחליף סיסמה (רק לשינוי סיסמה, /api/me ויציאה)."""
    sess = _session_from_header(authorization)
    if not sess:
        raise HTTPException(status_code=401, detail="נדרשת התחברות")
    return sess

def require_auth(authorization: str = Header(None)):
    sess = require_auth_any(authorization)
    if sess["must_change"]:
        raise HTTPException(status_code=403, detail="יש להחליף את הסיסמה הזמנית לפני שממשיכים")
    return sess

def require_staff(authorization: str = Header(None)):
    sess = require_auth(authorization)
    if sess["role"] not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="נדרשות הרשאות צוות")
    return sess

def require_admin(authorization: str = Header(None)):
    sess = require_auth(authorization)
    if sess["role"] != "admin":
        raise HTTPException(status_code=403, detail="נדרשות הרשאות מנהל")
    return sess

def _user_public(row):
    return {"id": row[0], "name": row[1], "email": row[2], "phone": row[3], "address": row[4], "role": row[5],
            "must_change_password": bool(row[6]) if len(row) > 6 else False}

def _fetch_user(cur, user_id):
    cur.execute("SELECT id, name, email, phone, address, role, must_change_password FROM users WHERE id = %s;", (user_id,))
    return cur.fetchone()

def _audit(actor, action, target_type=None, target_id=None, details=None):
    """רושם ביומן הפעולות. לא מפיל את הפעולה עצמה אם הרישום נכשל."""
    try:
        import json as _json
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_log (actor_id, actor_name, action, target_type, target_id, details) VALUES (%s,%s,%s,%s,%s,%s);",
            (actor.get("user_id") if actor else None, actor.get("name") if actor else None, action,
             target_type, target_id, _json.dumps(details, ensure_ascii=False) if details is not None else None),
        )
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[AUDIT] failed: {e}")

class RegisterBody(BaseModel):
    name: str
    email: str
    password: str
    phone: Optional[str] = None
    address: Optional[str] = None

class LoginBody(BaseModel):
    email: str
    password: str

def _issue_token(user_id, role=None):
    token = secrets.token_urlsafe(32)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE expires_at < NOW();")
    cur.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, NOW() + (%s || ' seconds')::interval);",
        (_token_hash(token), user_id, str(SESSION_TTL)),
    )
    conn.commit(); cur.close(); conn.close()
    return token

def _drop_sessions(cur, user_id, keep_token_hash=None):
    if keep_token_hash:
        cur.execute("DELETE FROM sessions WHERE user_id = %s AND token_hash <> %s;", (user_id, keep_token_hash))
    else:
        cur.execute("DELETE FROM sessions WHERE user_id = %s;", (user_id,))

@app.post("/api/register")
def register(body: RegisterBody):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="כתובת אימייל לא תקינה")
    if len(body.password) < 6:
        raise HTTPException(status_code=400, detail="הסיסמה חייבת להיות באורך 6 תווים לפחות")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE email = %s;", (email,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="כתובת האימייל כבר רשומה")
    cur.execute(
        "INSERT INTO users (name, email, phone, address, password_hash, role) VALUES (%s, %s, %s, %s, %s, 'customer') RETURNING id;",
        (body.name.strip(), email, body.phone, body.address, hash_password(body.password))
    )
    user_id = cur.fetchone()[0]
    conn.commit()
    user = _fetch_user(cur, user_id)
    cur.close(); conn.close()
    return {"token": _issue_token(user_id, "customer"), "user": _user_public(user)}

LOGIN_ATTEMPTS = defaultdict(list)

def check_rate_limit(ip: str, max_attempts: int = 10, window: int = 60):
    now = time_module.time()
    LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS[ip] if now - t < window]
    if len(LOGIN_ATTEMPTS[ip]) >= max_attempts:
        raise HTTPException(status_code=429, detail="יותר מדי ניסיונות התחברות. נסו שוב בעוד דקה.")

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15

@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)
    email = body.email.strip().lower()
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT id, password_hash, role, active, (locked_until IS NOT NULL AND locked_until > NOW()) FROM users WHERE email = %s;",
        (email,),
    )
    row = cur.fetchone()
    if row and row[4]:
        cur.close(); conn.close()
        raise HTTPException(status_code=429, detail=f"יותר מדי ניסיונות שגויים. החשבון נעול זמנית ל-{LOCK_MINUTES} דקות.")
    if not row or not verify_password(body.password, row[1]):
        if row:
            cur.execute(
                """
                UPDATE users SET
                    failed_logins = CASE WHEN failed_logins + 1 >= %s THEN 0 ELSE failed_logins + 1 END,
                    locked_until  = CASE WHEN failed_logins + 1 >= %s THEN NOW() + (%s || ' minutes')::interval ELSE locked_until END
                WHERE id = %s;
                """,
                (MAX_FAILED_LOGINS, MAX_FAILED_LOGINS, str(LOCK_MINUTES), row[0]),
            )
            conn.commit()
        cur.close(); conn.close()
        LOGIN_ATTEMPTS[ip].append(time_module.time())
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")
    if not row[3]:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="החשבון הושבת. יש לפנות למנהל.")
    cur.execute("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login = NOW() WHERE id = %s;", (row[0],))
    conn.commit()
    user = _fetch_user(cur, row[0])
    cur.close(); conn.close()
    LOGIN_ATTEMPTS.pop(ip, None)
    return {"token": _issue_token(row[0]), "user": _user_public(user)}

@app.post("/api/logout")
def logout(authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        conn = get_conn(); cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE token_hash = %s;", (_token_hash(authorization.split(" ", 1)[1]),))
        conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.get("/api/me")
def get_me(sess: dict = Depends(require_auth_any)):
    conn = get_conn(); cur = conn.cursor()
    user = _fetch_user(cur, sess["user_id"])
    cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    return _user_public(user)

# ===================== ניהול משתמשים וצוות (מנהלים בלבד) =====================
import string as _string

def _temp_password(n: int = 10) -> str:
    alphabet = "".join(c for c in _string.ascii_letters + _string.digits if c not in "0OolI1")
    return "".join(secrets.choice(alphabet) for _ in range(n))

def _user_admin_row(r):
    return {"id": r[0], "name": r[1], "email": r[2], "phone": r[3], "role": r[4], "active": bool(r[5]),
            "must_change_password": bool(r[6]),
            "created_at": r[7].strftime("%d/%m/%Y") if r[7] else "",
            "last_login": r[8].strftime("%d/%m/%Y %H:%M") if r[8] else ""}

_USER_ADMIN_COLS = "id, name, email, phone, role, active, must_change_password, created_at, last_login"

@app.get("/api/admin/users")
def admin_list_users(group: str = "staff", search: str = "", limit: int = 100, offset: int = 0,
                     sess: dict = Depends(require_admin)):
    where, params = [], []
    if group == "staff":
        where.append("role IN ('employee', 'admin')")
    elif group == "customers":
        where.append("role = 'customer'")
    if search.strip():
        where.append("(name ILIKE %s OR email ILIKE %s OR phone ILIKE %s)")
        like = f"%{search.strip()}%"; params += [like, like, like]
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(limit, 200)); offset = max(0, offset)
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM users {clause};", params)
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT {_USER_ADMIN_COLS} FROM users {clause} "
        "ORDER BY (role = 'admin') DESC, (role = 'employee') DESC, name NULLS LAST, id LIMIT %s OFFSET %s;",
        params + [limit, offset],
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return {"total": total, "items": [_user_admin_row(r) for r in rows]}

class StaffCreateBody(BaseModel):
    name: str
    email: str
    phone: Optional[str] = None
    role: str = "employee"

@app.post("/api/admin/users")
def admin_create_staff(body: StaffCreateBody, sess: dict = Depends(require_admin)):
    email = body.email.strip().lower()
    name = body.name.strip()
    if "@" not in email or not name:
        raise HTTPException(status_code=400, detail="צריך שם ואימייל תקין")
    if body.role not in STAFF_ROLES:
        raise HTTPException(status_code=400, detail="תפקיד לא תקין")
    temp = _temp_password()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, role FROM users WHERE email = %s;", (email,))
    existing = cur.fetchone()
    if existing:
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="האימייל כבר קיים במערכת. לקוחות הם לקריאה בלבד, ולכן כדי להפוך אותו לעובד יש ליצור לו משתמש צוות עם אימייל אחר.")
    cur.execute(
        "INSERT INTO users (name, email, phone, password_hash, role, must_change_password) VALUES (%s,%s,%s,%s,%s,TRUE) RETURNING id;",
        (name, email, (body.phone or "").strip() or None, hash_password(temp), body.role),
    )
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "user_create", "user", new_id, {"email": email, "role": body.role})
    return {"id": new_id, "email": email, "role": body.role, "temp_password": temp}

class UserUpdateBody(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None

@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserUpdateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name, phone, role, active FROM users WHERE id = %s FOR UPDATE;", (user_id,))
    cur_row = cur.fetchone()
    if not cur_row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    old_name, old_phone, old_role, old_active = cur_row
    if old_role == "customer":
        # לקוחות הם לקריאה בלבד: אי אפשר לשנות להם פרטים, תפקיד או סטטוס, כדי לא לפגוע בלקוח בטעות
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="לקוחות הם לקריאה בלבד. אי אפשר לשנות את הפרטים שלהם.")
    new_role = body.role if body.role is not None else old_role
    new_active = body.active if body.active is not None else old_active
    if new_role not in ROLES:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="תפקיד לא תקין")
    is_self = (user_id == sess["user_id"])
    if is_self and (new_role != old_role or new_active is False):
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="אי אפשר לשנות את התפקיד או להשבית את החשבון של עצמך")
    # תמיד חייב להישאר לפחות מנהל פעיל אחד
    if old_role == "admin" and old_active and (new_role != "admin" or not new_active):
        cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = TRUE AND id <> %s;", (user_id,))
        if cur.fetchone()[0] == 0:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail="חייב להישאר לפחות מנהל פעיל אחד")
    name = body.name.strip() if body.name is not None else old_name
    phone = (body.phone.strip() or None) if body.phone is not None else old_phone
    cur.execute("UPDATE users SET name = %s, phone = %s, role = %s, active = %s WHERE id = %s;",
                (name, phone, new_role, new_active, user_id))
    if new_role != old_role or new_active != old_active:
        _drop_sessions(cur, user_id)      # משנה תפקיד או מושבת: מתנתק מיד
    conn.commit(); cur.close(); conn.close()
    changes = {}
    if name != old_name: changes["name"] = [old_name, name]
    if phone != old_phone: changes["phone"] = [old_phone, phone]
    if new_role != old_role: changes["role"] = [old_role, new_role]
    if new_active != old_active: changes["active"] = [old_active, new_active]
    if changes:
        _audit(sess, "user_update", "user", user_id, changes)
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, sess: dict = Depends(require_admin)):
    temp = _temp_password()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT role FROM users WHERE id = %s;", (user_id,))
    _r = cur.fetchone()
    if _r and _r[0] == "customer":
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="לקוחות הם לקריאה בלבד. אי אפשר לאפס להם סיסמה.")
    cur.execute("UPDATE users SET password_hash = %s, must_change_password = TRUE, failed_logins = 0, locked_until = NULL "
                "WHERE id = %s RETURNING id;", (hash_password(temp), user_id))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    _drop_sessions(cur, user_id)
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "password_reset", "user", user_id)
    return {"ok": True, "temp_password": temp}

class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str

@app.post("/api/change-password")
def change_password(body: ChangePasswordBody, sess: dict = Depends(require_auth_any)):
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="הסיסמה החדשה חייבת להיות באורך 8 תווים לפחות")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="הסיסמה החדשה חייבת להיות שונה מהישנה")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE id = %s;", (sess["user_id"],))
    row = cur.fetchone()
    if not row or not verify_password(body.old_password, row[0]):
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="הסיסמה הנוכחית שגויה")
    cur.execute("UPDATE users SET password_hash = %s, must_change_password = FALSE WHERE id = %s;",
                (hash_password(body.new_password), sess["user_id"]))
    _drop_sessions(cur, sess["user_id"], keep_token_hash=sess["token_hash"])
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "password_change", "user", sess["user_id"])
    return {"ok": True}

@app.get("/api/admin/audit-log")
def admin_audit_log(limit: int = 100, sess: dict = Depends(require_admin)):
    limit = max(1, min(limit, 300))
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, created_at, actor_name, action, target_type, target_id, details FROM audit_log "
                "ORDER BY id DESC LIMIT %s;", (limit,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "at": r[1].strftime("%d/%m/%Y %H:%M") if r[1] else "", "actor": r[2] or "",
             "action": r[3], "target_type": r[4], "target_id": r[5], "details": r[6]} for r in rows]

# ===================== שעות עבודה ושכר =====================
MAX_SHIFT_HOURS = 24

def _month_bounds(month: Optional[str]):
    """'YYYY-MM' (שעון ישראל) -> (תחילת החודש, תחילת החודש הבא, המחרוזת)."""
    now = datetime.now(ISRAEL_TZ)
    try:
        y, m = (int(x) for x in month.split("-")) if month else (now.year, now.month)
        start = datetime(y, m, 1, tzinfo=ISRAEL_TZ)
    except Exception:
        raise HTTPException(status_code=400, detail="חודש לא תקין (פורמט YYYY-MM)")
    end = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=ISRAEL_TZ)
    return start, end, f"{y:04d}-{m:02d}"

def _il(dt):
    return dt.astimezone(ISRAEL_TZ) if dt else None

def _shift_dict(r, now=None):
    """r = (id, clock_in, clock_out, hourly_rate, note, edited_by_name)."""
    cin, cout = _il(r[1]), _il(r[2])
    minutes = None; pay = None
    if cout:
        minutes = max(0, int(round((cout - cin).total_seconds() / 60)))
        pay = round(minutes / 60 * float(r[3]), 2) if r[3] is not None else None
    return {
        "id": r[0],
        "date": cin.strftime("%d/%m/%Y"), "weekday": ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"][cin.weekday()],
        "in": cin.strftime("%H:%M"), "out": cout.strftime("%H:%M") if cout else None,
        "in_iso": cin.strftime("%Y-%m-%dT%H:%M"), "out_iso": cout.strftime("%Y-%m-%dT%H:%M") if cout else None,
        "minutes": minutes, "rate": float(r[3]) if r[3] is not None else None, "pay": pay,
        "note": r[4] or "", "edited": bool(r[5]), "edited_by": r[5] or "",
        "open": cout is None,
        "open_minutes": int((datetime.now(ISRAEL_TZ) - cin).total_seconds() // 60) if not cout else None,
    }

def _hours_payload(cur, user_id, month):
    start, end, month_str = _month_bounds(month)
    cur.execute(
        """
        SELECT s.id, s.clock_in, s.clock_out, s.hourly_rate, s.note, eb.name
        FROM shifts s LEFT JOIN users eb ON eb.id = s.edited_by
        WHERE s.user_id = %s AND s.clock_in >= %s AND s.clock_in < %s
        ORDER BY s.clock_in;
        """,
        (user_id, start, end),
    )
    items = [_shift_dict(r) for r in cur.fetchall()]
    total_min = sum(i["minutes"] or 0 for i in items)
    total_pay = round(sum(i["pay"] or 0 for i in items), 2)
    missing_rate = any(i["minutes"] and i["rate"] is None for i in items)
    return {"month": month_str, "items": items, "total_minutes": total_min, "total_pay": total_pay,
            "missing_rate": missing_rate}

# ---------- עובד / מנהל: שעון נוכחות ושעות שלי ----------
@app.get("/api/staff/clock")
def staff_clock_status(sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT s.id, s.clock_in, s.clock_out, s.hourly_rate, s.note, NULL FROM shifts s "
        "WHERE s.user_id = %s AND s.clock_out IS NULL;", (sess["user_id"],))
    row = cur.fetchone()
    cur.close(); conn.close()
    return {"open_shift": _shift_dict(row) if row else None, "name": sess["name"]}

@app.post("/api/staff/clock/in")
def staff_clock_in(sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT hourly_rate FROM users WHERE id = %s;", (sess["user_id"],))
    rate = cur.fetchone()[0]
    try:
        cur.execute("INSERT INTO shifts (user_id, clock_in, hourly_rate) VALUES (%s, NOW(), %s) RETURNING id;",
                    (sess["user_id"], rate))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="כבר רשומה כניסה פתוחה. צריך לסמן יציאה קודם.")
    cur.close(); conn.close()
    return {"ok": True}

@app.post("/api/staff/clock/out")
def staff_clock_out(sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE shifts SET clock_out = NOW() WHERE user_id = %s AND clock_out IS NULL RETURNING id;",
                (sess["user_id"],))
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=409, detail="אין כניסה פתוחה. צריך לסמן כניסה קודם.")
    return {"ok": True}

@app.get("/api/staff/my-hours")
def staff_my_hours(month: Optional[str] = None, sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    data = _hours_payload(cur, sess["user_id"], month)
    cur.execute("SELECT hourly_rate FROM users WHERE id = %s;", (sess["user_id"],))
    r = cur.fetchone()[0]
    cur.close(); conn.close()
    data["current_rate"] = float(r) if r is not None else None
    return data

# ---------- מנהל: שעות ושכר של כולם ----------
@app.get("/api/admin/hours-summary")
def admin_hours_summary(month: Optional[str] = None, sess: dict = Depends(require_admin)):
    start, end, month_str = _month_bounds(month)
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.name, u.email, u.role, u.active, u.hourly_rate,
               COALESCE(SUM(EXTRACT(EPOCH FROM (s.clock_out - s.clock_in)) / 60) FILTER (WHERE s.clock_out IS NOT NULL), 0),
               COALESCE(SUM(EXTRACT(EPOCH FROM (s.clock_out - s.clock_in)) / 3600 * s.hourly_rate)
                        FILTER (WHERE s.clock_out IS NOT NULL AND s.hourly_rate IS NOT NULL), 0),
               COUNT(s.id) FILTER (WHERE s.clock_out IS NULL),
               COUNT(s.id) FILTER (WHERE s.clock_out IS NOT NULL AND s.hourly_rate IS NULL)
        FROM users u
        LEFT JOIN shifts s ON s.user_id = u.id AND s.clock_in >= %s AND s.clock_in < %s
        WHERE u.role IN ('employee', 'admin')
        GROUP BY u.id
        ORDER BY u.active DESC, u.name NULLS LAST, u.id;
        """,
        (start, end),
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    items = [{"id": r[0], "name": r[1], "email": r[2], "role": r[3], "active": bool(r[4]),
              "hourly_rate": float(r[5]) if r[5] is not None else None,
              "minutes": int(round(float(r[6]))), "pay": round(float(r[7]), 2),
              "open_shifts": r[8], "shifts_without_rate": r[9]} for r in rows]
    return {"month": month_str, "items": items,
            "total_pay": round(sum(i["pay"] for i in items), 2),
            "total_minutes": sum(i["minutes"] for i in items)}

@app.get("/api/admin/hours/{user_id}")
def admin_user_hours(user_id: int, month: Optional[str] = None, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT name, email, hourly_rate FROM users WHERE id = %s;", (user_id,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    data = _hours_payload(cur, user_id, month)
    cur.close(); conn.close()
    data.update({"user_id": user_id, "name": u[0], "email": u[1], "current_rate": float(u[2]) if u[2] is not None else None})
    return data

class RateBody(BaseModel):
    hourly_rate: Optional[float] = None

@app.put("/api/admin/users/{user_id}/rate")
def admin_set_rate(user_id: int, body: RateBody, sess: dict = Depends(require_admin)):
    rate = body.hourly_rate
    if rate is not None and (rate < 0 or rate > 1000):
        raise HTTPException(status_code=400, detail="תעריף שעתי לא תקין")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT hourly_rate FROM users WHERE id = %s FOR UPDATE;", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    old = float(row[0]) if row[0] is not None else None
    cur.execute("UPDATE users SET hourly_rate = %s WHERE id = %s;", (rate, user_id))
    filled = 0
    if rate is not None:
        # משמרות שנרשמו כשעוד לא היה תעריף מקבלות אותו. משמרות עם תעריף קודם לא משתנות.
        cur.execute("UPDATE shifts SET hourly_rate = %s WHERE user_id = %s AND hourly_rate IS NULL;", (rate, user_id))
        filled = cur.rowcount
        # משמרת פתוחה כרגע שנפתחה בתעריף אחר: מעדכנים גם אותה, כדי שלא תיחשב לפי תעריף ישן
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "rate_change", "user", user_id, {"hourly_rate": [old, rate], "filled_shifts": filled})
    return {"ok": True, "filled_shifts": filled}

def _parse_local_dt(val: Optional[str]):
    """'YYYY-MM-DDTHH:MM' בשעון ישראל."""
    if not val:
        return None
    try:
        dt = datetime.strptime(val[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        raise HTTPException(status_code=400, detail="פורמט תאריך ושעה לא תקין")
    return dt.replace(tzinfo=ISRAEL_TZ)

def _validate_shift_times(cur, user_id, cin, cout, exclude_id=None):
    if not cin:
        raise HTTPException(status_code=400, detail="חסרה שעת כניסה")
    if cout is not None:
        if cout <= cin:
            raise HTTPException(status_code=400, detail="שעת היציאה חייבת להיות אחרי שעת הכניסה")
        if (cout - cin).total_seconds() > MAX_SHIFT_HOURS * 3600:
            raise HTTPException(status_code=400, detail=f"משמרת לא יכולה להיות ארוכה מ-{MAX_SHIFT_HOURS} שעות")
    cur.execute(
        """
        SELECT 1 FROM shifts
        WHERE user_id = %s AND (%s::int IS NULL OR id <> %s)
          AND clock_in < COALESCE(%s::timestamptz, 'infinity'::timestamptz)
          AND COALESCE(clock_out, 'infinity'::timestamptz) > %s;
        """,
        (user_id, exclude_id, exclude_id, cout, cin),
    )
    if cur.fetchone():
        raise HTTPException(status_code=409, detail="המשמרת חופפת למשמרת אחרת של אותו עובד")

class ShiftCreateBody(BaseModel):
    user_id: int
    clock_in: str
    clock_out: Optional[str] = None
    hourly_rate: Optional[float] = None
    note: Optional[str] = None

@app.post("/api/admin/shifts")
def admin_create_shift(body: ShiftCreateBody, sess: dict = Depends(require_admin)):
    cin, cout = _parse_local_dt(body.clock_in), _parse_local_dt(body.clock_out)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT hourly_rate FROM users WHERE id = %s;", (body.user_id,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    rate = body.hourly_rate if body.hourly_rate is not None else u[0]
    try:
        _validate_shift_times(cur, body.user_id, cin, cout)
        cur.execute(
            "INSERT INTO shifts (user_id, clock_in, clock_out, hourly_rate, note, created_by) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id;",
            (body.user_id, cin, cout, rate, (body.note or "").strip() or None, sess["user_id"]))
        new_id = cur.fetchone()[0]
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="לעובד הזה כבר יש כניסה פתוחה")
    except HTTPException:
        conn.rollback(); cur.close(); conn.close(); raise
    cur.close(); conn.close()
    _audit(sess, "shift_create", "shift", new_id, {"user_id": body.user_id, "in": body.clock_in, "out": body.clock_out})
    return {"id": new_id}

class ShiftUpdateBody(BaseModel):
    clock_in: Optional[str] = None
    clock_out: Optional[str] = None      # מחרוזת ריקה = משמרת פתוחה
    hourly_rate: Optional[float] = None
    note: Optional[str] = None

@app.patch("/api/admin/shifts/{shift_id}")
def admin_update_shift(shift_id: int, body: ShiftUpdateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, clock_in, clock_out, hourly_rate, note FROM shifts WHERE id = %s FOR UPDATE;", (shift_id,))
    old = cur.fetchone()
    if not old:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המשמרת לא נמצאה")
    user_id = old[0]
    cin = _parse_local_dt(body.clock_in) if body.clock_in else old[1]
    cout = old[2] if body.clock_out is None else _parse_local_dt(body.clock_out)
    rate = body.hourly_rate if body.hourly_rate is not None else old[3]
    note = old[4] if body.note is None else ((body.note.strip()) or None)
    try:
        _validate_shift_times(cur, user_id, cin, cout, exclude_id=shift_id)
        cur.execute("UPDATE shifts SET clock_in=%s, clock_out=%s, hourly_rate=%s, note=%s, edited_by=%s, edited_at=NOW() WHERE id=%s;",
                    (cin, cout, rate, note, sess["user_id"], shift_id))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="לעובד הזה כבר יש כניסה פתוחה")
    except HTTPException:
        conn.rollback(); cur.close(); conn.close(); raise
    cur.close(); conn.close()
    fmt = lambda d: _il(d).strftime("%Y-%m-%d %H:%M") if d else None
    _audit(sess, "shift_update", "shift", shift_id,
           {"user_id": user_id, "in": [fmt(old[1]), fmt(cin)], "out": [fmt(old[2]), fmt(cout)],
            "rate": [float(old[3]) if old[3] is not None else None, float(rate) if rate is not None else None]})
    return {"ok": True}

@app.delete("/api/admin/shifts/{shift_id}")
def admin_delete_shift(shift_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM shifts WHERE id = %s RETURNING user_id, clock_in, clock_out;", (shift_id,))
    row = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="המשמרת לא נמצאה")
    fmt = lambda d: _il(d).strftime("%Y-%m-%d %H:%M") if d else None
    _audit(sess, "shift_delete", "shift", shift_id, {"user_id": row[0], "in": fmt(row[1]), "out": fmt(row[2])})
    return {"ok": True}

def _parse_time(val, default):
    try:
        h, m = val.split(":")
        return time(int(h), int(m))
    except Exception:
        return default

@app.get("/api/store-status")
def get_store_status():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = 'delivery_override';")
    row = cur.fetchone()
    override = row[0] if row else "auto"

    now_dt = datetime.now(ISRAEL_TZ)
    today_date = now_dt.date()
    now_time = now_dt.time()
    js_weekday = (now_dt.weekday() + 1) % 7 

    cur.execute("SELECT title, is_closed, opening_time, closing_time, note FROM special_days WHERE holiday_date = %s;", (today_date,))
    special = cur.fetchone()

    note_text = ""
    is_open = False

    if special:
        title, is_closed, op_str, cl_str, note = special
        note_text = note or f"היום {title}"
        if not is_closed:
            op_t = _parse_time(op_str, time(9, 0))
            cl_t = _parse_time(cl_str, time(18, 0))
            is_open = op_t <= now_time <= cl_t
            hours_str = f"{op_str} - {cl_str}"
        else:
            hours_str = "סגור לרגל החג"
    else:
        cur.execute("SELECT is_closed, opening_time, closing_time FROM weekly_hours WHERE day_of_week = %s;", (js_weekday,))
        week_data = cur.fetchone()
        if week_data and not week_data[0]:
            op_t = _parse_time(week_data[1], time(9, 0))
            cl_t = _parse_time(week_data[2], time(18, 0))
            is_open = op_t <= now_time <= cl_t
            hours_str = f"{week_data[1]} - {week_data[2]}"
        else:
            hours_str = "סגור היום"

    cur.close(); conn.close()

    if override == "on":
        delivery_active = True
    elif override == "off":
        delivery_active = False
    else:
        delivery_active = is_open

    msg = "החנות פתוחה כעת" if is_open else "החנות סגורה כרגע"
    if note_text:
        msg = f"{note_text} | {msg}"

    return {
        "store_open": is_open,
        "delivery_available": delivery_active,
        "opening_hours": hours_str,
        "message": msg
    }

@app.get("/api/schedule")
def get_public_schedule():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT day_of_week, day_name, is_closed, opening_time, closing_time FROM weekly_hours ORDER BY day_of_week;")
    weekly = [{"day_of_week": r[0], "day_name": r[1], "is_closed": r[2], "opening_time": r[3], "closing_time": r[4]} for r in cur.fetchall()]

    today_date = datetime.now(ISRAEL_TZ).date()
    cur.execute("SELECT holiday_date, title, is_closed, opening_time, closing_time, note FROM special_days WHERE holiday_date >= %s ORDER BY holiday_date;", (today_date,))
    special = [{"holiday_date": str(r[0]), "title": r[1], "is_closed": r[2], "opening_time": r[3], "closing_time": r[4], "note": r[5] or ""} for r in cur.fetchall()]
    
    cur.close(); conn.close()
    return {"weekly": weekly, "special": special}

class WeeklyDayUpdate(BaseModel):
    day_of_week: int
    is_closed: bool
    opening_time: str
    closing_time: str

class SpecialDayCreate(BaseModel):
    holiday_date: str
    title: str
    is_closed: bool
    opening_time: Optional[str] = "09:00"
    closing_time: Optional[str] = "18:00"
    note: Optional[str] = ""

class OverrideBody(BaseModel):
    override: str

@app.get("/api/admin/schedule")
def admin_get_schedule(sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT day_of_week, day_name, is_closed, opening_time, closing_time FROM weekly_hours ORDER BY day_of_week;")
    weekly = [{"day_of_week": r[0], "day_name": r[1], "is_closed": r[2], "opening_time": r[3], "closing_time": r[4]} for r in cur.fetchall()]

    cur.execute("SELECT id, holiday_date, title, is_closed, opening_time, closing_time, note FROM special_days ORDER BY holiday_date;")
    special = [{"id": r[0], "holiday_date": str(r[1]), "title": r[2], "is_closed": r[3], "opening_time": r[4], "closing_time": r[5], "note": r[6] or ""} for r in cur.fetchall()]

    cur.execute("SELECT value FROM settings WHERE key = 'delivery_override';")
    row = cur.fetchone()
    override = row[0] if row else "auto"

    cur.close(); conn.close()
    return {"weekly": weekly, "special": special, "delivery_override": override}

@app.put("/api/admin/schedule/weekly")
def admin_update_weekly(days: List[WeeklyDayUpdate], sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    for d in days:
        cur.execute(
            "UPDATE weekly_hours SET is_closed = %s, opening_time = %s, closing_time = %s WHERE day_of_week = %s;",
            (d.is_closed, d.opening_time, d.closing_time, d.day_of_week)
        )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.post("/api/admin/schedule/special")
def admin_add_special(body: SpecialDayCreate, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO special_days (holiday_date, title, is_closed, opening_time, closing_time, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (holiday_date) DO UPDATE
            SET title = EXCLUDED.title, is_closed = EXCLUDED.is_closed, opening_time = EXCLUDED.opening_time, closing_time = EXCLUDED.closing_time, note = EXCLUDED.note
            RETURNING id;
            """,
            (body.holiday_date, body.title, body.is_closed, body.opening_time, body.closing_time, body.note)
        )
        conn.commit()
    except Exception as e:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="שגיאה בהזנת תאריך מיוחד")
    cur.close(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/schedule/special/{special_id}")
def admin_delete_special(special_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM special_days WHERE id = %s;", (special_id,))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.put("/api/admin/schedule/override")
def admin_set_override(body: OverrideBody, sess: dict = Depends(require_admin)):
    if body.override not in ("auto", "on", "off"):
        raise HTTPException(status_code=400, detail="ערך לא תקין")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO settings (key, value) VALUES ('delivery_override', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;", (body.override,))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.get("/api/admin/subcategories-flat")
def admin_get_subcategories_flat(sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        SELECT sub.id, p.name || ' > ' || sub.name AS full_name
        FROM Categories sub
        JOIN Categories p ON sub.parent_id = p.id
        ORDER BY p.name, sub.name;
        """
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]

@app.post("/api/admin/upload-image")
async def upload_image(file: UploadFile = File(...), sess: dict = Depends(require_admin)):
    try:
        ext = file.filename.split(".")[-1]
        filename = f"{uuid.uuid4().hex}.{ext}"
        file_path = os.path.join(IMAGES_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(await file.read())
        return {"url": f"/images/{filename}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail="שגיאה בהעלאת התמונה")

# --- Products & Categories ---

def _serialize_products(rows):
    data = []
    for p in rows:
        cat_ids = list(p[10]) if len(p) > 10 and p[10] else []
        if p[6] and p[6] not in cat_ids:
            cat_ids.append(p[6])
        
        is_on_sale = p[11] if len(p) > 11 else False
        sale_price = float(p[12]) if len(p) > 12 and p[12] is not None else None
        sale_label = p[13] if len(p) > 13 else None
            
        data.append({
            "id": p[0], "name": p[1], "price": float(p[2]), "in_stock": p[3] > 0, 
            "category": p[4], "image": p[5] if p[5] else PLACEHOLDER, "category_id": p[6],
            "weight_grams": p[7], "units_per_package": p[8], "notes": p[9],
            "category_ids": cat_ids,
            "is_on_sale": bool(is_on_sale),
            "sale_price": sale_price,
            "sale_label": sale_label,
        })
    return data

def _fetch_categories(where_clause, params=()):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        f"""
        WITH RECURSIVE tree AS (
            SELECT id, id AS root FROM Categories
            UNION ALL
            SELECT c.id, t.root FROM Categories c JOIN tree t ON c.parent_id = t.id
        ),
        counts AS (
            SELECT t.root AS cat_id, COUNT(p.id) AS product_count
            FROM tree t LEFT JOIN Products p ON p.category_id = t.id
            GROUP BY t.root
        )
        SELECT c.id, c.name, c.parent_id, c.image_url,
               COALESCE(cnt.product_count, 0),
               EXISTS (SELECT 1 FROM Categories ch WHERE ch.parent_id = c.id)
        FROM Categories c
        LEFT JOIN counts cnt ON cnt.cat_id = c.id
        {where_clause}
        ORDER BY c.sort_order, c.id;
        """,
        params,
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "name": r[1], "parent_id": r[2], "image": r[3] or PLACEHOLDER, "product_count": r[4], "has_children": r[5]} for r in rows]

@app.get("/api/categories")
def get_main_categories():
    return _fetch_categories("WHERE c.parent_id IS NULL")

@app.get("/api/categories/{category_id}/subcategories")
def get_subcategories(category_id: int):
    return _fetch_categories("WHERE c.parent_id = %s", (category_id,))

@app.get("/api/products")
def get_products():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url, 
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id),
               p.is_on_sale, p.sale_price, p.sale_label
        FROM Products p 
        LEFT JOIN Categories c ON p.category_id = c.id 
        WHERE p.category_id IS NOT NULL 
        ORDER BY p.stock_quantity DESC, p.id;
    """)
    data = _serialize_products(cur.fetchall()); cur.close(); conn.close()
    return data

@app.get("/api/products/new")
def get_new_products(limit: int = 8):
    """מוצרים שנוספו לאחרונה (45 הימים האחרונים), לשורת "חדש בחנות" בדף הבית."""
    limit = max(1, min(limit, 20))
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url,
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id),
               p.is_on_sale, p.sale_price, p.sale_label
        FROM Products p
        LEFT JOIN Categories c ON p.category_id = c.id
        WHERE p.category_id IS NOT NULL
          AND p.created_at IS NOT NULL
          AND p.created_at > NOW() - INTERVAL '45 days'
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT %s;
    """, (limit,))
    data = _serialize_products(cur.fetchall()); cur.close(); conn.close()
    return data

@app.get("/api/categories/{category_id}/products")
def get_category_products(category_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        WITH RECURSIVE subtree AS (
            SELECT id FROM Categories WHERE id = %s
            UNION ALL
            SELECT c.id FROM Categories c JOIN subtree s ON c.parent_id = s.id
        )
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url, 
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id),
               p.is_on_sale, p.sale_price, p.sale_label
        FROM Products p 
        LEFT JOIN Categories c ON p.category_id = c.id 
        WHERE p.category_id IN (SELECT id FROM subtree) 
           OR EXISTS (
               SELECT 1 FROM product_categories pc 
               WHERE pc.product_id = p.id AND pc.category_id IN (SELECT id FROM subtree)
           )
        ORDER BY p.stock_quantity DESC, p.id;
        """,
        (category_id,),
    )
    data = _serialize_products(cur.fetchall()); cur.close(); conn.close()
    return data

class ProductUpdateBody(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = None
    category_ids: Optional[List[int]] = None
    stock_quantity: Optional[int] = None
    weight_grams: Optional[int] = None
    units_per_package: Optional[int] = None
    image_url: Optional[str] = None
    notes: Optional[str] = None
    is_on_sale: Optional[bool] = None
    sale_label: Optional[str] = None


@app.get("/api/admin/products")
def admin_list_products(search: str = "", only_unsorted: bool = False,
                        limit: int = 30, offset: int = 0,
                        sess: dict = Depends(require_admin)):
    """רשימת מוצרים מדופדפת לפאנל הניהול, עם חיפוש וסינון."""
    limit = max(1, min(limit, 100))
    conn = get_conn(); cur = conn.cursor()
    conditions, params = [], []
    if search:
        conditions.append("p.name ILIKE %s")
        params.append(f"%{search}%")
    if only_unsorted:
        conditions.append("p.category_id IS NULL")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    cur.execute(f"SELECT COUNT(*) FROM Products p {where};", params)
    total = cur.fetchone()[0]
    cur.execute(f"""
        SELECT p.id, p.name, p.price, p.stock_quantity, p.image_url, p.category_id,
               (SELECT array_agg(pc.category_id) FROM product_categories pc WHERE pc.product_id = p.id) AS cat_ids
        FROM Products p {where}
        ORDER BY p.id
        LIMIT %s OFFSET %s;
    """, params + [limit, offset])
    items = []
    for r in cur.fetchall():
        cat_ids = list(r[6]) if r[6] else []
        if r[5] and r[5] not in cat_ids:
            cat_ids.append(r[5])
        items.append({
            "id": r[0], "name": r[1], "price": float(r[2]),
            "stock_quantity": r[3],
            "image": r[4] if r[4] else PLACEHOLDER,
            "category_id": r[5], "category_ids": cat_ids,
        })
    cur.close(); conn.close()
    return {"total": total, "items": items, "limit": limit, "offset": offset}

@app.patch("/api/admin/products/{product_id}")
def admin_update_product(product_id: int, body: ProductUpdateBody, sess: dict = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    conn = get_conn(); cur = conn.cursor()
    
    cat_ids = fields.pop("category_ids", None)

    if cat_ids is not None:
        fields["category_id"] = cat_ids[0] if cat_ids else None

    if fields:
        sets = ", ".join(f"{k} = %s" for k in fields)
        cur.execute(f"UPDATE Products SET {sets} WHERE id = %s;", list(fields.values()) + [product_id])
        
    if cat_ids is not None:
        cur.execute("DELETE FROM product_categories WHERE product_id = %s;", (product_id,))
        for cid in cat_ids[:2]:
            cur.execute("INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;", (product_id, cid))

    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

class ProductCreateBody(BaseModel):
    name: str
    price: float
    category_ids: List[int] = [] 
    stock_quantity: Optional[int] = 100
    weight_grams: Optional[int] = None
    units_per_package: Optional[int] = None
    image_url: Optional[str] = None
    notes: Optional[str] = None
    is_on_sale: Optional[bool] = False
    sale_label: Optional[str] = None

@app.post("/api/admin/products")
def admin_create_product(body: ProductCreateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    primary_cat = body.category_ids[0] if body.category_ids else None
    
    cur.execute(
        """
        INSERT INTO Products (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes, is_on_sale, sale_label)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (body.name.strip(), primary_cat, body.price, body.stock_quantity, body.weight_grams, body.units_per_package, body.image_url, body.notes, body.is_on_sale, body.sale_label)
    )
    product_id = cur.fetchone()[0]

    for cat_id in body.category_ids[:2]:
        cur.execute(
            "INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (product_id, cat_id)
        )

    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "product_id": product_id}

@app.delete("/api/admin/products/{product_id}")
def admin_delete_product(product_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM Products WHERE id = %s RETURNING id;", (product_id,))
    deleted = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="המוצר לא נמצא")
    return {"ok": True}

@app.post("/api/admin/products/{product_id}/duplicate")
def admin_duplicate_product(product_id: int, sess: dict = Depends(require_admin)):
    """
    משכפל מוצר קיים (כולל תמונה, מחיר, קטגוריות, הערות וכו') למוצר חדש.
    שימושי למוצרים שקיימים במלא גרסאות (למשל אבקת צבע מאכל בכמה צבעים) —
    משכפלים פעם אחת, ואז רק משנים שם/גרם/צבע בכל עותק במקום להקליד הכל מחדש.
    השם החדש מקבל אוטומטית "(עותק)" בסוף כדי שיהיה קל להבחין ולערוך.
    """
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        SELECT name, category_id, price, stock_quantity, weight_grams,
               units_per_package, image_url, notes, is_on_sale, sale_label
        FROM Products WHERE id = %s;
        """,
        (product_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המוצר לא נמצא")

    (name, category_id, price, stock_quantity, weight_grams,
     units_per_package, image_url, notes, is_on_sale, sale_label) = row

    cur.execute(
        """
        INSERT INTO Products
            (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes, is_on_sale, sale_label)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (f"{name} (עותק)", category_id, price, stock_quantity, weight_grams,
         units_per_package, image_url, notes, is_on_sale, sale_label),
    )
    new_id = cur.fetchone()[0]

    # מעתיקים גם את כל שיוכי הקטגוריות הנוספים (טבלת הקישור, לא רק הקטגוריה הראשית)
    cur.execute("SELECT category_id FROM product_categories WHERE product_id = %s;", (product_id,))
    for (cid,) in cur.fetchall():
        cur.execute(
            "INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (new_id, cid),
        )

    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "product_id": new_id}

@app.get("/api/products/{product_id}")
def get_product_detail(product_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url,
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id),
               p.is_on_sale, p.sale_price, p.sale_label
        FROM Products p LEFT JOIN Categories c ON p.category_id = c.id
        WHERE p.id = %s;
        """,
        (product_id,),
    )
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="המוצר לא נמצא")
        
    cur.execute("SELECT category_id FROM product_categories WHERE product_id = %s;", (product_id,))
    cats = [row[0] for row in cur.fetchall()]
    if not cats and r[6]:
        cats = [r[6]]
        
    cur.close(); conn.close()
    return {
        "id": r[0], "name": r[1], "price": float(r[2]), "in_stock": r[3] > 0,
        "category": r[4], "image": r[5] if r[5] else PLACEHOLDER, "category_id": r[6],
        "weight_grams": r[7], "units_per_package": r[8],
        "notes": r[9],
        "category_ids": cats,
        "is_on_sale": bool(r[11]),
        "sale_label": r[13]
    }

@app.get("/api/categories/tree")
def get_categories_tree():
    mains = _fetch_categories("WHERE c.parent_id IS NULL")
    for main in mains:
        if main["has_children"]:
            main["subcategories"] = _fetch_categories("WHERE c.parent_id = %s", (main["id"],))
        else:
            main["subcategories"] = []
    return mains

CONTACT_DEFAULTS = {
    "contact_phone": "054-9881998",
    "contact_address": "יוני נתניהו 21, גבעת שמואל",
    "contact_email": "pinukimmam@gmail.com",
    "contact_whatsapp": "972549881998",
}

def _get_contact():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings WHERE key = ANY(%s);", (list(CONTACT_DEFAULTS.keys()),))
    data = dict(cur.fetchall())
    cur.close(); conn.close()
    return {
        "phone": data.get("contact_phone", CONTACT_DEFAULTS["contact_phone"]),
        "address": data.get("contact_address", CONTACT_DEFAULTS["contact_address"]),
        "email": data.get("contact_email", CONTACT_DEFAULTS["contact_email"]),
        "whatsapp": data.get("contact_whatsapp", CONTACT_DEFAULTS["contact_whatsapp"]),
    }

@app.get("/api/contact")
def get_contact():
    return _get_contact()

class ContactBody(BaseModel):
    phone: str
    address: str
    email: str
    whatsapp: str

@app.put("/api/admin/contact")
def update_contact(body: ContactBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    for key, val in [("contact_phone", body.phone), ("contact_address", body.address),
                     ("contact_email", body.email), ("contact_whatsapp", body.whatsapp)]:
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;", (key, val))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

# --- שאלות ותשובות נפוצות (FAQ) ---

@app.get("/api/faq")
def get_faq():
    """שאלות ותשובות מפורסמות בלבד — לתצוגה בדף הבית."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, question, answer FROM faq_items WHERE published = TRUE ORDER BY sort_order, id;")
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "question": r[1], "answer": r[2]} for r in rows]

@app.get("/api/admin/faq")
def admin_list_faq(sess: dict = Depends(require_admin)):
    """כל הפריטים כולל כאלה שעדיין לא פורסמו, עם מונה השאלות מהצ'אט."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT id, topic_key, question, answer, hit_count, published, sort_order
        FROM faq_items ORDER BY published DESC, hit_count DESC, sort_order, id;
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    return [
        {"id": r[0], "topic_key": r[1], "question": r[2], "answer": r[3],
         "hit_count": r[4], "published": r[5], "sort_order": r[6]}
        for r in rows
    ]

class FaqCreateBody(BaseModel):
    question: str
    answer: str
    published: bool = True
    sort_order: int = 100

@app.post("/api/admin/faq")
def admin_create_faq(body: FaqCreateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO faq_items (question, answer, published, sort_order) VALUES (%s, %s, %s, %s) RETURNING id;",
        (body.question.strip(), body.answer.strip(), body.published, body.sort_order),
    )
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "id": new_id}

class FaqUpdateBody(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    published: Optional[bool] = None
    sort_order: Optional[int] = None

@app.patch("/api/admin/faq/{faq_id}")
def admin_update_faq(faq_id: int, body: FaqUpdateBody, sess: dict = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return {"ok": True}
    conn = get_conn(); cur = conn.cursor()
    sets = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE faq_items SET {sets}, updated_at = NOW() WHERE id = %s;", list(fields.values()) + [faq_id])
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/faq/{faq_id}")
def admin_delete_faq(faq_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM faq_items WHERE id = %s RETURNING id;", (faq_id,))
    deleted = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="הפריט לא נמצא")
    return {"ok": True}

class CategoryCreateBody(BaseModel):
    name: str
    parent_id: Optional[int] = None

class CategoryUpdateBody(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    sort_order: Optional[int] = None

@app.post("/api/admin/categories")
def admin_create_category(body: CategoryCreateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO Categories (name, parent_id, sort_order) VALUES (%s, %s, 999) RETURNING id;", (body.name, body.parent_id))
    new_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    return {"id": new_id, "name": body.name, "parent_id": body.parent_id}

@app.patch("/api/admin/categories/{category_id}")
def admin_update_category(category_id: int, body: CategoryUpdateBody, sess: dict = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="לא נשלחו שדות לעדכון")
    conn = get_conn(); cur = conn.cursor()
    sets = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(f"UPDATE Categories SET {sets} WHERE id = %s RETURNING id;", list(fields.values()) + [category_id])
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="הקטגוריה לא נמצאה")
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.delete("/api/admin/categories/{category_id}")
def admin_delete_category(category_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM Categories WHERE id = %s RETURNING id;", (category_id,))
    deleted = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="הקטגוריה לא נמצאה")
    return {"ok": True, "deleted": category_id}

@app.get("/api/products/on-sale")
def get_products_on_sale():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url, 
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id),
               p.is_on_sale, p.sale_price, p.sale_label
        FROM Products p 
        LEFT JOIN Categories c ON p.category_id = c.id 
        WHERE p.is_on_sale = TRUE AND p.category_id IS NOT NULL
        ORDER BY p.id;
    """)
    data = _serialize_products(cur.fetchall()); cur.close(); conn.close()
    return data

# --- ייבוא מוצרים מאקסל של וולט (תצוגה מקדימה ואז אישור) ---
# לא מוחק כלום: מוסיף מוצרים וקטגוריות חדשים בלבד. מוצר שכבר קיים (לפי שם) נשאר כמו שהוא.
_WOLT_PSEUDO_CATS = {"חדש בחנות", "חדש_בחנות", "מוצרים מובילים", "מוצרים_מובילים",
                     "חדש בחנות ומוצרים מובילים", "חדש_בחנות_ומוצרים_מובילים"}

def _wolt_price(raw, divide: bool, rounding: str):
    import math
    v = float(raw)
    if divide:
        v = v / 1.3
    v = round(v, 2)
    if rounding == "nine":      # ה-x.9 הקרוב כלפי מעלה (10.52 -> 10.9)
        v = max(0, math.ceil(round(v - 0.9, 2))) + 0.9
    elif rounding == "tenth":   # עד 10 אגורות כלפי מעלה
        v = math.ceil(round(v * 10, 6)) / 10
    return round(v, 2)

def _norm_name(n):
    return " ".join(str(n).split()).strip().lower()

@app.post("/api/admin/import-wolt")
async def admin_import_wolt(
    file: UploadFile = File(...),
    mode: str = "preview",
    divide: bool = True,
    rounding: str = "nine",
    update_prices: bool = False,
    sess: dict = Depends(require_admin),
):
    import pandas as pd, io
    if mode not in ("preview", "apply") or rounding not in ("nine", "tenth", "none"):
        raise HTTPException(status_code=400, detail="פרמטר לא תקין")
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="הקובץ גדול מדי")
    try:
        df_cat = pd.read_excel(io.BytesIO(raw), sheet_name="categories")
        df_off = pd.read_excel(io.BytesIO(raw), sheet_name="offers")
        for col in ("name", "subcategories"):
            assert col in df_cat.columns
        for col in ("name", "price", "category_id"):
            assert col in df_off.columns
    except Exception:
        raise HTTPException(status_code=400, detail="זה לא נראה כמו קובץ הייצוא של וולט (חסרים הגיליונות categories ו-offers).")

    conn = get_conn(); cur = conn.cursor()
    try:
        # ---------- קטגוריות ----------
        # בקובץ חדש יותר של וולט אין עמודת id בגיליון הקטגוריות. לכן מזהים לפי שם:
        # שם קטגוריה לכל id נלקח מהגיליון (אם יש id) או מעמודת category_id של המוצרים ("שם::id").
        sheet_names = [str(n).strip() for n in df_cat["name"] if pd.notna(n)]
        id_to_name = {}
        if "id" in df_cat.columns:
            for _, r in df_cat.iterrows():
                if pd.notna(r["id"]):
                    id_to_name[str(r["id"]).strip()] = str(r["name"]).strip()
        for v in df_off["category_id"].dropna():
            for part in str(v).split(","):
                nm, _, wid = part.strip().rpartition("::")
                wid = wid.strip()
                if wid and wid not in id_to_name:
                    nm = nm.strip().replace("_", " ")
                    id_to_name[wid] = nm
        parent_of, order_in = {}, {}
        for _, r in df_cat.iterrows():
            if pd.notna(r["subcategories"]):
                for i, sub in enumerate(str(r["subcategories"]).split(",")):
                    child = id_to_name.get(sub.strip())
                    if child and child != str(r["name"]).strip():
                        parent_of[child] = str(r["name"]).strip(); order_in[child] = i
        pseudo_names = {n for n in set(sheet_names) | set(id_to_name.values()) if n.replace("_", " ") in {x.replace("_", " ") for x in _WOLT_PSEUDO_CATS}}
        node_names = [n for n in dict.fromkeys(sheet_names + list(id_to_name.values())) if n not in pseudo_names]
        wid_by_name = {}
        for wid, nm in id_to_name.items():
            wid_by_name.setdefault(nm, wid)

        name_to_db, cats_created, cats_matched = {}, 0, 0
        remaining = list(node_names)
        guard = 0
        while remaining and guard < 20:
            guard += 1
            still = []
            for nm in remaining:
                pn = parent_of.get(nm)
                if pn in pseudo_names:
                    pn = None
                if pn is not None and pn not in name_to_db:
                    if pn in node_names:
                        still.append(nm); continue
                    pn = None
                parent_db = name_to_db.get(pn) if pn else None
                wid = wid_by_name.get(nm)
                row = None
                if wid:
                    cur.execute("SELECT id FROM Categories WHERE wolt_id = %s;", (wid,))
                    row = cur.fetchone()
                if not row:
                    cur.execute("SELECT id, wolt_id FROM Categories WHERE lower(trim(name)) = lower(%s) "
                                "AND parent_id IS NOT DISTINCT FROM %s LIMIT 1;", (nm, parent_db))
                    row = cur.fetchone()
                    if row and row[1] is None and wid:
                        cur.execute("UPDATE Categories SET wolt_id = %s WHERE id = %s AND NOT EXISTS (SELECT 1 FROM Categories WHERE wolt_id = %s);", (wid, row[0], wid))
                if row:
                    name_to_db[nm] = row[0]; cats_matched += 1
                else:
                    cur.execute("INSERT INTO Categories (wolt_id, name, parent_id, sort_order) VALUES (%s,%s,%s,%s) RETURNING id;",
                                (wid, nm, parent_db, order_in.get(nm, 999)))
                    name_to_db[nm] = cur.fetchone()[0]; cats_created += 1
            remaining = still

        # ---------- מוצרים: איחוד כפילויות (אותו שם ואותו מחיר) ----------
        merged = {}
        for _, r in df_off.iterrows():
            if pd.isna(r["name"]) or pd.isna(r["price"]):
                continue
            nm = " ".join(str(r["name"]).split())
            key = (_norm_name(nm), round(float(r["price"]), 2))
            ids = []
            for c in ([] if pd.isna(r["category_id"]) else str(r["category_id"]).split(",")):
                wid = c.strip().split("::")[-1].strip()
                cname = id_to_name.get(wid)
                if cname and cname in name_to_db and name_to_db[cname] not in ids:
                    ids.append(name_to_db[cname])
            m = merged.get(key)
            if not m:
                def num(col):
                    return int(r[col]) if col in r.index and pd.notna(r[col]) else None
                img = str(r["images"]).split(",")[0].strip() if "images" in r.index and pd.notna(r["images"]) else None
                merged[key] = {
                    "name": nm, "wolt_price": float(r["price"]), "cats": ids,
                    "stock": 0 if str(r.get("inventory_mode")) == "forced_out_of_stock" else 100,
                    "weight": num("weight_in_grams"), "units": num("number_of_units"),
                    "image": img or None,
                    "notes": str(r["description"]) if "description" in r.index and pd.notna(r["description"]) else None,
                }
            else:
                for c in ids:
                    if c not in m["cats"]:
                        m["cats"].append(c)

        cur.execute("SELECT id, name FROM Products;")
        existing = {}
        for pid, pn in cur.fetchall():
            existing.setdefault(_norm_name(pn), pid)

        added = skipped = updated = uncategorized = multi = 0
        samples = []
        for (nk, _), m in merged.items():
            price = _wolt_price(m["wolt_price"], divide, rounding)
            if nk in existing:   # existing = מה שהיה באתר לפני הייבוא
                skipped += 1
                if update_prices:
                    cur.execute("UPDATE Products SET price = %s WHERE id = %s;", (price, existing[nk])); updated += 1
                continue
            primary = m["cats"][0] if m["cats"] else None
            cur.execute(
                "INSERT INTO Products (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;",
                (m["name"], primary, price, m["stock"], m["weight"], m["units"], m["image"], m["notes"]))
            new_id = cur.fetchone()[0]
            for cid in m["cats"]:
                cur.execute("INSERT INTO product_categories (product_id, category_id) VALUES (%s,%s) ON CONFLICT DO NOTHING;", (new_id, cid))
            added += 1
            uncategorized += 0 if m["cats"] else 1
            multi += 1 if len(m["cats"]) > 1 else 0
            if len(samples) < 12:
                samples.append({"name": m["name"], "wolt_price": m["wolt_price"], "price": price, "categories": len(m["cats"])})

        if mode == "apply":
            conn.commit()
        else:
            conn.rollback()
    except HTTPException:
        conn.rollback(); raise
    except Exception as e:
        conn.rollback()
        print(f"[IMPORT-WOLT] failed: {e}")
        raise HTTPException(status_code=500, detail="הייבוא נכשל ולא שונה כלום באתר.")
    finally:
        cur.close(); conn.close()
    result = {"mode": mode, "products_in_file": len(merged), "added": added, "skipped_existing": skipped,
              "prices_updated": updated, "categories_created": cats_created, "categories_matched": cats_matched,
              "uncategorized": uncategorized, "multi_category": multi, "samples": samples}
    if mode == "apply":
        _audit(sess, "wolt_import", "products", None, {k: v for k, v in result.items() if k != "samples"})
    return result


# --- שיוך מהיר של מבצע מוכן ---
class QuickDealBody(BaseModel):
    product_ids: List[int]
    deal_type: str  # "6b55" | "4b25" | "8b40" | "clear"

DEAL_MAP = {
    "6b55": ("6 ב-₪55", None),
    "4b25": ("4 ב-₪25", None),
    "8b40": ("8 ב-₪40", None),
    "10b27": ("10 ב-₪27", None),
}

@app.post("/api/admin/quick-deal")
def admin_quick_deal(body: QuickDealBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    if body.deal_type == "clear":
        for pid in body.product_ids:
            cur.execute("UPDATE Products SET is_on_sale = FALSE, sale_label = NULL, sale_price = NULL WHERE id = %s;", (pid,))
    elif body.deal_type in DEAL_MAP:
        label, sale_price = DEAL_MAP[body.deal_type]
        for pid in body.product_ids:
            cur.execute("UPDATE Products SET is_on_sale = TRUE, sale_label = %s, sale_price = %s WHERE id = %s;", (label, sale_price, pid))
    else:
        raise HTTPException(status_code=400, detail="סוג מבצע לא מוכר")
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "updated": len(body.product_ids)}





# --- SEO: Sitemap + דפי מוצר לגוגל ---
from fastapi.responses import Response

@app.get("/sitemap.xml")
def sitemap():
    """מפת אתר דינמית — גוגל קורא אותה כדי לדעת אילו דפים קיימים."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM Products WHERE category_id IS NOT NULL ORDER BY id;")
    products = cur.fetchall()
    cur.close(); conn.close()

    base = "https://www.bakeparty.co.il"
    urls = [f"""  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"""]
    # רק דפי מוצר אמיתיים. (הקטגוריות אין להן דף URL משלהן, אז לא מכניסים אותן —
    # אחרת נוצרות כתובות שבורות/כפולות שגוגל מנסה לסרוק ונכשל.)
    for pid, pname in products:
        urls.append(f"""  <url><loc>{base}/product/{pid}</loc><changefreq>weekly</changefreq><priority>0.6</priority></url>""")
    
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>"""
    return Response(content=xml, media_type="application/xml")


@app.get("/product/{product_id}")
def product_seo_page(product_id: int):
    """דף מוצר עם SEO מלא — schema.org, meta tags, Open Graph. גוגל סורק את זה."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url, p.notes "
        "FROM Products p LEFT JOIN Categories c ON p.category_id = c.id WHERE p.id = %s;",
        (product_id,),
    )
    p = cur.fetchone()
    cur.close(); conn.close()
    if not p:
        raise HTTPException(status_code=404, detail="מוצר לא נמצא")
    
    name, price, in_stock, category, image, desc = p[1], float(p[2]), p[3] > 0, p[4] or "", p[5] or "", p[6] or ""
    availability = "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock"
    
    html = f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name} | Bake & Party - אפייה ומסיבות</title>
    <meta name="description" content="{name} - {desc[:120] if desc else category + ' | Bake & Party אפייה ומסיבות בגבעת שמואל'}">
    <meta property="og:title" content="{name} | Bake & Party">
    <meta property="og:description" content="{desc[:200] if desc else 'אפייה ומסיבות בגבעת שמואל'}">
    <meta property="og:image" content="{image}">
    <meta property="og:type" content="product">
    <meta property="og:url" content="https://www.bakeparty.co.il/product/{product_id}">
    <meta property="product:price:amount" content="{price}">
    <meta property="product:price:currency" content="ILS">
    <link rel="canonical" href="https://www.bakeparty.co.il/product/{product_id}">
    <script type="application/ld+json">
    {{
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "{name}",
        "image": "{image}",
        "description": "{desc}",
        "brand": {{"@type": "Brand", "name": "Bake & Party"}},
        "offers": {{
            "@type": "Offer",
            "price": "{price}",
            "priceCurrency": "ILS",
            "availability": "{availability}",
            "seller": {{
                "@type": "LocalBusiness",
                "name": "Bake & Party - ממלכת הגלידות מבית פינוקים",
                "address": {{
                    "@type": "PostalAddress",
                    "streetAddress": "יוני נתניהו 21",
                    "addressLocality": "גבעת שמואל",
                    "addressCountry": "IL"
                }}
            }}
        }}
    }}
    </script>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #FFF9F5; color: #4A2E35; margin: 0; direction: rtl; }}
        .container {{ max-width: 700px; margin: 40px auto; padding: 20px; text-align: center; }}
        .prod-img {{ max-width: 300px; border-radius: 16px; margin: 20px auto; }}
        .price {{ font-size: 2em; color: #E05276; font-weight: bold; margin: 16px 0; }}
        .cat {{ color: #9B7B82; margin: 10px 0; }}
        .desc {{ line-height: 1.7; margin: 16px 0; }}
        .back {{ display: inline-block; margin-top: 20px; background: #FF85A2; color: #fff; padding: 12px 30px;
            border-radius: 20px; text-decoration: none; font-weight: bold; }}
        .back:hover {{ background: #E05276; }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" style="text-decoration:none;"><h1 style="color:#E05276;">Bake & Party</h1></a>
        <p class="cat">{category}</p>
        <img src="{image}" alt="{name}" class="prod-img" onerror="this.style.display='none'">
        <h2>{name}</h2>
        <div class="price">₪{price:.2f}</div>
        {"<p class='desc'>" + desc + "</p>" if desc else ""}
        <p>{"✅ במלאי" if in_stock else "❌ אזל מהמלאי"}</p>
        <a href="/" class="back">→ לכל המוצרים</a>
    </div>
</body>
</html>"""
    return Response(content=html, media_type="text/html")


# ============================================================
#  🤖 עוזר ה-AI של Bake & Party   (POST /api/ai/chat)
# ============================================================
# מה צריך כדי שזה יעבוד בשרת (Railway):
#   1. להוסיף Variable בשם  ANTHROPIC_API_KEY  עם מפתח מ-console.anthropic.com
#   2. (אופציונלי) AI_MODEL — לבחירת מודל אחר. ברירת המחדל היא מודל מהיר וזול.
#   3. requirements.txt כבר כולל את החבילה "anthropic" — Railway יתקין לבד.
# בלי מפתח ה-endpoint לא קורס: הוא מחזיר הודעה ידידותית שמפנה לטלפון/וואטסאפ.

AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5-20251001")
AI_MAX_PRODUCTS = 40          # כמה מוצרים תואמים לצרף להקשר
AI_MAX_MSG_CHARS = 1500       # אורך הודעה מקסימלי מהמשתמש (הגנה)

# ============================================================
#  🔍 בדיקת "מה אזל/חזר למלאי" מול וולט — למנהלים בלבד, admin.html
# ============================================================
# הגרסה הקודמת קראה את דף וולט החי (scraping) — התבררה כלא אמינה, כי וולט
# טוענים חלק מהמוצרים רק תוך כדי גלילה (JavaScript), וקריאת HTML רגיל לא
# תופסת את כל הקטלוג. גרם לדיווחי-שווא על מוצרים שבפועל כן היו במלאי.
#
# הגרסה הזו משתמשת במקור אמת אמיתי מוולט: **אותו קובץ אקסל** שכבר מורידים
# מוולט ומשמש את import_data.py. בעמודה "inventory_mode" וולט עצמם מסמנים
# forced_out_of_stock לכל מוצר שאזל אצלם — בלי צורך "לנחש" מקריאת אתר.
#
# ההשוואה דו-כיוונית:
#   1. מוצר שאצלנו "במלאי" אבל בקובץ וולט מסומן forced_out_of_stock
#      → כנראה אזל בוולט, כדאי לסמן גם אצלנו.
#   2. מוצר שאצלנו "אזל" אבל בקובץ וולט הוא כן זמין
#      → כנראה חזר למלאי, כדאי להחזיר גם אצלנו.

def _normalize_name_tokens(name):
    """מנקה שם מוצר למילים משמעותיות להשוואה (מסיר מספרים/יחידות/פיסוק)."""
    import re
    text = re.sub(r"[\d.,%]+\s*(g|kg|ml|l|יח['\"]?|גרם|מ\"ל|מ״ל|ליטר)?", " ", name, flags=re.IGNORECASE)
    text = re.sub(r"[^\u0590-\u05FFA-Za-z\s]", " ", text)
    return {t for t in text.split() if len(t) >= 2}


def _best_name_match(target_tokens, candidates_with_tokens):
    """מוצא את ההתאמה הכי טובה (Jaccard) מתוך רשימת (name, tokens). מחזיר (name, similarity) או (None, 0)."""
    best_name, best_sim = None, 0
    for cand_name, cand_tokens in candidates_with_tokens:
        if not cand_tokens or not target_tokens:
            continue
        sim = len(target_tokens & cand_tokens) / len(target_tokens | cand_tokens)
        if sim > best_sim:
            best_name, best_sim = cand_name, sim
    return best_name, best_sim


def compare_stock_with_wolt_file(file_path):
    """
    משווה בין קובץ האקסל שהורד מוולט (גיליון 'offers') לבין המוצרים שלנו.
    "לא זמין בוולט" נקבע לפי **שני** סימנים ביחד (או-אחד-מהם מספיק):
      - inventory_mode == 'forced_out_of_stock'  (אזל זמנית מהמלאי)
      - enabled == False  (מוצר מושבת/לא פעיל בוולט לגמרי — יכול להיות
        לא מסומן forced_out_of_stock בכלל, אבל עדיין לא ניתן לרכישה שם)
    בלי לבדוק גם את enabled, מוצרים מושבתים-אבל-לא-"אזל-רשמית" היו "נעלמים"
    מהבדיקה ונראים בטעות כזמינים.

    מחזיר dict עם שלוש רשימות:
    - to_mark_out_of_stock: אצלנו "במלאי", בוולט לא זמין (אזל או מושבת)
    - to_mark_back_in_stock: אצלנו "אזל", בוולט כן זמין ופעיל
    - new_products_not_in_our_site: קיים בוולט, לא נמצא אצלנו בכלל
      (בלי קשר לסטטוס המלאי שלנו — פשוט לא קיים בטבלת Products שלנו)
    כל פריט: {"our_id", "our_name", "wolt_name", "similarity"} (לשתי הראשונות)
    או {"wolt_name", "price", "available"} (לשלישית, אין "אצלנו" בכלל).

    זורק ValueError עם הודעה ברורה בעברית אם הקובץ מהסוג הלא נכון —
    יש כמה סוגי ייצוא שונים בוולט, ורק אחד מהם (זה עם גיליון "offers"
    ועמודת "inventory_mode") מתאים לבדיקת מלאי.
    """
    import pandas as pd

    try:
        df = pd.read_excel(file_path, sheet_name="offers")
    except ValueError:
        raise ValueError(
            "זה לא קובץ הזמינות הנכון — חסר גיליון בשם 'offers'. "
            "בוולט יש כמה סוגי ייצוא שונים; ודאי שהורדת את הדוח שכולל את נתוני המלאי "
            "(אותו סוג קובץ ששימש בזמנו את import_data.py), לא דוח אחר כמו סדר הצגת מוצרים."
        )

    if "inventory_mode" not in df.columns or "name" not in df.columns:
        raise ValueError(
            "הקובץ נפתח בהצלחה, אבל חסרות בו עמודות חיוניות (name / inventory_mode). "
            "כנראה שזה סוג ייצוא אחר מוולט — צריך את הדוח עם נתוני זמינות המלאי."
        )

    has_enabled_col = "enabled" in df.columns

    wolt_items = []  # (name, tokens, is_out_of_stock, price, image_url, weight_grams)
    for _, row in df.iterrows():
        name = str(row.get("name", "")).strip()
        if not name or name.lower() == "nan":
            continue
        is_forced_out = str(row.get("inventory_mode", "")) == "forced_out_of_stock"
        is_disabled = has_enabled_col and (row.get("enabled") is False)
        is_out = is_forced_out or is_disabled
        price = row.get("price")
        price = float(price) if pd.notna(price) else None
        # וולט לפעמים מחזירים כמה תמונות מופרדות בפסיק — לוקחים את הראשונה (כמו ב-import_data.py)
        raw_images = row.get("images")
        image_url = str(raw_images).split(",")[0].strip() if pd.notna(raw_images) else None
        weight = row.get("weight_in_grams")
        weight = int(weight) if pd.notna(weight) else None
        wolt_items.append((name, _normalize_name_tokens(name), is_out, price, image_url, weight))

    wolt_available = [(n, t) for n, t, out, p, im, w in wolt_items if not out]
    wolt_out_of_stock = [(n, t) for n, t, out, p, im, w in wolt_items if out]

    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name, stock_quantity FROM Products ORDER BY name;")
    our_products = cur.fetchall()
    cur.close(); conn.close()

    our_tokens_all = [_normalize_name_tokens(name) for _, name, _ in our_products]

    SIM_THRESHOLD = 0.7
    to_mark_out, to_mark_back = [], []

    for prod_id, prod_name, stock in our_products:
        our_tokens = _normalize_name_tokens(prod_name)
        if not our_tokens:
            continue

        if (stock or 0) > 0:
            # אצלנו במלאי — האם וולט מסמנים אותו כאזל?
            match_name, sim = _best_name_match(our_tokens, wolt_out_of_stock)
            if sim >= SIM_THRESHOLD:
                # לוודא שאין התאמה טובה יותר ברשימת הזמינים (אם יש התנגשות שם)
                avail_match, avail_sim = _best_name_match(our_tokens, wolt_available)
                if avail_sim < sim:
                    to_mark_out.append({
                        "our_id": prod_id, "our_name": prod_name,
                        "wolt_name": match_name, "similarity": round(sim, 2),
                    })
        else:
            # אצלנו אזל — האם וולט מסמנים אותו כזמין?
            match_name, sim = _best_name_match(our_tokens, wolt_available)
            if sim >= SIM_THRESHOLD:
                out_match, out_sim = _best_name_match(our_tokens, wolt_out_of_stock)
                if out_sim < sim:
                    to_mark_back.append({
                        "our_id": prod_id, "our_name": prod_name,
                        "wolt_name": match_name, "similarity": round(sim, 2),
                    })

    # מוצרים שקיימים בוולט אבל אין להם שום התאמה אצלנו בכלל (לא "אזל", ממש חסרים)
    our_candidates = list(zip([name for _, name, _ in our_products], our_tokens_all))
    new_products = []
    for name, tokens, is_out, price, image_url, weight in wolt_items:
        if not tokens:
            continue
        _, best_sim = _best_name_match(tokens, our_candidates)
        if best_sim < SIM_THRESHOLD:
            new_products.append({
                "wolt_name": name,
                "price": price,
                "available": not is_out,
                "image_url": image_url,
                "weight_grams": weight,
            })

    return {
        "to_mark_out_of_stock": to_mark_out,
        "to_mark_back_in_stock": to_mark_back,
        "new_products_not_in_our_site": new_products,
    }


@app.post("/api/admin/wolt-stock-check")
async def admin_wolt_stock_check(file: UploadFile = File(...), sess: dict = Depends(require_admin)):
    """
    מקבל את קובץ האקסל שהורד מוולט (אותו קובץ ש-import_data.py משתמש בו)
    ומחזיר השוואה דו-כיוונית מול המלאי שלנו. לא נוגע במסד הנתונים —
    רק מחזיר הצעות; העדכון בפועל נעשה דרך admin.html בלחיצת כפתור נפרדת
    (משתמש ב-endpoint הקיים של עדכון מוצר).
    """
    tmp_path = f"/tmp/wolt_check_{uuid.uuid4().hex}.xlsx"
    try:
        with open(tmp_path, "wb") as f:
            f.write(await file.read())
        result = compare_stock_with_wolt_file(tmp_path)
        return result
    except ValueError as e:
        # שגיאות ולידציה ברורות (סוג קובץ לא נכון) — ההודעה כבר מנוסחת בעברית ומוכנה להצגה
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"שגיאה לא צפויה בקריאת הקובץ: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


AI_SYSTEM_PROMPT = """את/ה העוזר/ת החכם/ה והחמוד/ה של "Bake & Party" (אפייה ומסיבות) —
חנות משפחתית בגבעת שמואל שמוכרת חומרי גלם לאפייה, אביזרים, קישוטים ומוצרים למסיבות.

התפקיד שלך:
1. להמליץ ללקוחות מה כדאי להכין ואילו מוצרים *מהחנות שלנו* הכי מתאימים למה שהם רוצים (עוגה, יום הולדת, בר מצווה וכו').
2. לתת רעיונות למתכונים פשוטים ולציין אילו מוצרים שלנו משתלבים בהם.
3. להגיד ללקוח *היכן בחנות* (באיזו קטגוריה) למצוא מוצר.

## אורך התשובה — חשוב מאוד
- ברוב המקרים: 2–5 שורות. אף פעם לא "קיר טקסט".
- אל תשפוך/כי רשימת מוצרים ארוכה. הצע/י 2–4 פריטים רלוונטיים בלבד,
  ואז הצע/י להרחיב: "רוצה שאראה עוד אפשרויות?"
- סיים/י תמיד במשפט שלם. אל תשאיר/י משפט קטוע באמצע.

## בירור צורך ממוקד
- אם הבקשה כללית (למשל "יש קישוטים לבר מצווה?") — אל תענה/י מיד עם הכול.
- שאל/י שאלה אחת ממוקדת שתעזור לצמצם, לדוגמה:
  • "זה לעוגה עצמה או לקישוט השולחן?"
  • "יש סגנון שאת/ה אוהב/ת — קלאסי, מודרני או צבעוני?"
  • "לכמה אנשים?"
- שאלה אחת בכל פעם, לא שלוש. אחרי שהלקוח עונה — תן/י המלצה ממוקדת.
- אם הלקוח כבר היה ספציפי — אל תשאל/י סתם, פשוט תענה/י.

## עזרה בזיהוי מוצר לפי תיאור (לא רק שם)
לפעמים לקוח זוכר איך מוצר נראה/משמש אבל לא את השם המדויק (למשל "מחפש/ת את הרוטב הוורוד לפונדו שוקולד").
- ברשימת "מוצרים שנמצאו" בהמשך יש גם שדה "תיאור" לכל מוצר — תמיד תסתכל/י גם עליו, לא רק על השם, כדי לנסות להתאים.
- אם מצאת/ה התאמה סבירה — הציעי אותה, אבל בנימוס ציין/י שזו ההשערה הכי טובה שלך ("נראה לי שהתכוונת ל...").
- אם התיאור מעורפל מדי ומצאת/ה כמה אפשרויות — שאל/י שאלה ממוקדת אחת שתעזור להבדיל ביניהן, במקום לנחש.
- אם לא נמצאה שום התאמה סבירה בנתונים — אמור/י בכנות שלא הצלחת לזהות, והצע/י ליצור קשר טלפוני/וואטסאפ עם התיאור (יעזרו לזהות ידנית).

כללים חשובים:
- ענה/י בעברית, בחום, בקצרה ולעניין. אפשר אימוג'י פה ושם, לא להגזים.
- הסתמך/י אך ורק על נתוני החנות שמצורפים בהמשך. אל תמציא/י מוצרים, מחירים או מלאי שלא מופיעים בנתונים.
- אם מוצר לא מופיע בנתונים שקיבלת — אל תבטיח/י שיש אותו. אמור/י שאינך בטוח/ה, והצע/י ליצור קשר טלפוני/וואטסאפ או לבדוק בוולט (הפרטים בהקשר).
- אם שואלים על "דפי סוכר" — ספר/י שהכלי לבדיקת גודל דף סוכר לפני הדפסה נמצא בפיתוח ויתווסף בקרוב 🙂
- אם מנהל/ת שואל/ת על השוואת מלאי מול וולט — הפנה/י לכלי הייעודי בפאנל הניהול (admin.html), שם אפשר להעלות את קובץ האקסל של וולט ולקבל השוואה מדויקת. לא בצ'אט.
- אל תמציא/י כתובות אתר. השתמש/י רק בקישורים שמופיעים בהקשר.
- סיים/י כל תשובה במשפט קצר אחד שמזמין את הצעד הבא.

## התמקדות בנושאי החנות בלבד — חשוב מאוד
את/ה כאן *רק* בשביל נושאים שקשורים לחנות: מוצרים, מתכונים, אפייה, מסיבות, קטגוריות, שעות, משלוחים, יצירת קשר.
- ברכות/נימוסים ("היי", "תודה", "מה שלומך") — לגיטימי לגמרי, ענה/י בחום כרגיל.
- אם השאלה **לגמרי לא קשורה** לחנות (למשל: פוליטיקה, עזרה בתכנות, שאלות אישיות על מישהו, כל דבר שאין לו שום קשר לאפייה/מסיבות/החנות) —
  אל תענה/י על השאלה עצמה. הסבר/י בנימוס: "מצטער/ת, אני יכול/ה לעזור רק בנושאים שקשורים לחנות שלנו 🧁 יש לך שאלה על מוצרים, מתכונים או משהו למסיבה?"

## סיווג נושא (topic) — לצורך מעקב שאלות נפוצות
בנוסף לתשובה, סווג/י את שאלת הלקוח *האחרונה* לאחד מהנושאים הקבועים האלה בלבד:
- ordering — איך מזמינים / הזמנה דרך האתר
- delivery — משלוחים, עלות מינימלית למשלוח
- hours — שעות פתיחה/סגירה
- location — מיקום החנות, הגעה עצמאית
- recommendation — המלצה מה לקנות לאירוע/מתכון
- sugar_sheets — דפי סוכר להדפסה
- prices — מחירים/מחירון כללי
- availability — האם מוצר מסוים במלאי
- contact — איך יוצרים קשר
- other — כל דבר אחר, כולל ברכות/נימוסים או שאלות לא-קשורות לחנות

## סימון "קשור לחנות" (on_topic)
סמן/י true אם השאלה קשורה לחנות (כולל ברכות/נימוסים), ו-false רק אם היא **לגמרי לא קשורה** לחנות (ראה סעיף למעלה).

## פורמט תשובה — קריטי
החזר/י תמיד **אך ורק** אובייקט JSON תקין, בלי טקסט נוסף, בלי ```, בפורמט המדויק הזה:
{"reply": "<התשובה המלאה ללקוח, בעברית>", "topic": "<אחד מהמפתחות למעלה, באנגלית>", "on_topic": <true או false>}
"""

FAQ_TOPIC_KEYS = {
    "ordering", "delivery", "hours", "location", "recommendation",
    "sugar_sheets", "prices", "availability", "contact",
}
FAQ_AUTO_PUBLISH_THRESHOLD = 5

# --- הגנה מפני שימוש לרעה בצ'אט ---
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "30"))          # הודעות מקסימום ליום למשתמש
AI_OFFTOPIC_BLOCK_THRESHOLD = 5                                       # כמה הודעות לא-קשורות ברצף עד חסימה
AI_BLOCK_HOURS = 24                                                   # לכמה שעות החסימה תקפה


class AiChatMsg(BaseModel):
    role: str
    content: str


class AiChatBody(BaseModel):
    message: str
    history: Optional[List[AiChatMsg]] = None


def _ai_extract_keywords(text, min_len=2, max_words=8):
    import re
    words = re.findall(r"[\u0590-\u05FFA-Za-z0-9']{%d,}" % min_len, text or "")
    seen = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:max_words]


def _ai_build_context(user_message):
    """בונה טקסט הקשר: מבנה הקטגוריות + מוצרים שתואמים למילים בהודעה + פרטי קשר."""
    parts = []
    try:
        conn = get_conn()
        cur = conn.cursor()

        # מבנה החנות: ראשיות -> תתי-קטגוריות
        cur.execute("SELECT id, name, parent_id FROM Categories ORDER BY parent_id NULLS FIRST, sort_order, id;")
        rows = cur.fetchall()
        mains = [(r[0], r[1]) for r in rows if r[2] is None]
        subs = defaultdict(list)
        for r in rows:
            if r[2] is not None:
                subs[r[2]].append(r[1])
        struct_lines = []
        for mid, mname in mains:
            if subs.get(mid):
                struct_lines.append(f"- {mname}: " + ", ".join(subs[mid]))
            else:
                struct_lines.append(f"- {mname}")
        if struct_lines:
            parts.append("קטגוריות החנות:\n" + "\n".join(struct_lines))

        # מוצרים שתואמים למילים בהודעת המשתמש — מחפשים גם בשם וגם בתיאור/הערות
        # המוצר (לא רק בשם), כדי שהחיפוש יעבוד גם כשלקוח מתאר מוצר במילים
        # משלו בלי לזכור את השם המדויק (למשל "הרוטב הוורוד לפונדו שוקולד").
        kws = _ai_extract_keywords(user_message)
        if kws:
            like = " OR ".join(["(p.name ILIKE %s OR p.notes ILIKE %s)"] * len(kws))
            params = []
            for k in kws:
                params += [f"%{k}%", f"%{k}%"]
            cur.execute(
                f"""
                SELECT p.name, c.name, p.stock_quantity, p.price, p.notes
                FROM Products p LEFT JOIN Categories c ON p.category_id = c.id
                WHERE {like}
                ORDER BY p.stock_quantity DESC, p.id
                LIMIT %s;
                """,
                params + [AI_MAX_PRODUCTS],
            )
            matched = []
            for name, cat, stock, price, notes in cur.fetchall():
                status = "במלאי" if (stock or 0) > 0 else "אזל מהמלאי"
                line = f"• {name} — קטגוריה: {cat or 'ללא'} — {status} — ₪{float(price):.0f}"
                if notes:
                    snippet = notes.strip().replace("\n", " ")[:120]
                    line += f" — תיאור: {snippet}"
                matched.append(line)
            if matched:
                parts.append("מוצרים שנמצאו אצלנו שקשורים לשאלה (כולל התאמה לפי תיאור, לא רק שם):\n" + "\n".join(matched))
            else:
                parts.append("לא נמצאו מוצרים אצלנו שתואמים ישירות למילים בשאלה (לא בשם ולא בתיאור).")

        cur.close()
        conn.close()
    except Exception as e:
        parts.append(f"(שגיאה בטעינת נתוני החנות: {e})")

    # פרטי קשר
    try:
        c = _get_contact()
        parts.append(
            "פרטי קשר לחנות: טלפון {phone} | וואטסאפ https://wa.me/{wa} | "
            "כתובת {addr} | וולט https://wolt.com/he/isr/petah-tikva/venue/pinookim-givat-shmuel".format(
                phone=c["phone"], wa=c["whatsapp"], addr=c["address"]
            )
        )
    except Exception:
        pass

    return "\n\n".join(parts)


def _track_faq_topic(topic_key):
    """
    כל פעם ששאלה בצ'אט מסווגת לנושא קבוע, סופרים אותה. ברגע שנושא מסוים
    חוצה את FAQ_AUTO_PUBLISH_THRESHOLD פעמים, הוא עולה אוטומטית לדף הבית
    (published=TRUE) — עם השאלה/תשובה שכבר כתובות מראש בטבלה, לא טקסט
    גולמי מהצ'אט. עוטפים בטיפול שגיאות כדי שתקלה כאן לעולם לא תפיל את הצ'אט.
    """
    if not topic_key or topic_key not in FAQ_TOPIC_KEYS:
        return
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(
            "UPDATE faq_items SET hit_count = hit_count + 1, updated_at = NOW() "
            "WHERE topic_key = %s RETURNING hit_count, published;",
            (topic_key,),
        )
        row = cur.fetchone()
        if row:
            hit_count, published = row
            if hit_count >= FAQ_AUTO_PUBLISH_THRESHOLD and not published:
                cur.execute("UPDATE faq_items SET published = TRUE WHERE topic_key = %s;", (topic_key,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[FAQ] tracking error: {e}")


def _ai_check_guard(user_id):
    """
    בודק לפני קריאה ל-AI: האם המשתמש חסום זמנית (בגלל הודעות לא-קשורות ברצף),
    או שחרג ממכסת ההודעות היומית. מחזיר (allowed: bool, message: str|None).
    """
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT ai_blocked_until FROM users WHERE id = %s;", (user_id,))
        row = cur.fetchone()
        if row and row[0] and row[0] > datetime.now():
            remaining_h = max(1, int((row[0] - datetime.now()).total_seconds() // 3600) + 1)
            return False, (
                f"הצ'אט חסום זמנית בעקבות כמה הודעות לא-קשורות לחנות ברצף. "
                f"אפשר לנסות שוב בעוד כ-{remaining_h} שעות, או לפנות אלינו ישירות בטלפון/וואטסאפ 🙏"
            )

        today = date.today()
        cur.execute("SELECT message_count FROM ai_usage WHERE user_id = %s AND usage_date = %s;", (user_id, today))
        row = cur.fetchone()
        if row and row[0] >= AI_DAILY_LIMIT:
            return False, (
                "הגעת למכסת ההודעות היומית לצ'אט החכם 🙂 אפשר להמשיך מחר, "
                "או בינתיים ליצור איתנו קשר ישירות בטלפון/וואטסאפ."
            )
        return True, None
    finally:
        cur.close(); conn.close()


def _ai_record_usage_and_ontopic(user_id, on_topic):
    """
    רץ אחרי כל הודעה מוצלחת: סופר אותה במכסה היומית, ומעדכן את רצף
    ההודעות הלא-קשורות (מאפס ברגע שיש הודעה קשורה, מגדיל אחרת) — וחוסם
    זמנית אם הרצף חוצה את הסף.
    """
    conn = get_conn(); cur = conn.cursor()
    try:
        today = date.today()
        cur.execute(
            "INSERT INTO ai_usage (user_id, usage_date, message_count) VALUES (%s, %s, 1) "
            "ON CONFLICT (user_id, usage_date) DO UPDATE SET message_count = ai_usage.message_count + 1;",
            (user_id, today),
        )
        if on_topic is False:
            cur.execute(
                "UPDATE users SET ai_offtopic_streak = ai_offtopic_streak + 1 WHERE id = %s RETURNING ai_offtopic_streak;",
                (user_id,),
            )
            streak = cur.fetchone()[0]
            if streak >= AI_OFFTOPIC_BLOCK_THRESHOLD:
                cur.execute(
                    "UPDATE users SET ai_blocked_until = %s, ai_offtopic_streak = 0 WHERE id = %s;",
                    (datetime.now() + timedelta(hours=AI_BLOCK_HOURS), user_id),
                )
        elif on_topic is True:
            cur.execute("UPDATE users SET ai_offtopic_streak = 0 WHERE id = %s;", (user_id,))
        conn.commit()
    except Exception as e:
        print(f"[AI] usage/guard tracking error: {e}")
    finally:
        cur.close(); conn.close()


def _ai_log_chat(user_id, message, reply, topic=None, on_topic=None, status="ok", error_text=None):
    """שומר שורה ביומן השיחות. לעולם לא שובר את הצ'אט אם השמירה נכשלת."""
    try:
        conn = get_conn(); cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO ai_chat_log (user_id, message, reply, topic, on_topic, status, error_text) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s);",
                (user_id, message, reply, topic or None, on_topic, status, (error_text or None) and str(error_text)[:800]),
            )
            conn.commit()
        finally:
            cur.close(); conn.close()
    except Exception as e:
        print(f"[AI] chat-log error: {e}")


@app.get("/api/admin/ai-log")
def admin_ai_log(limit: int = 200, only_failed: bool = False, sess: dict = Depends(require_admin)):
    """השאלות האחרונות ששאלו את ה-AI (חדשות ראשונות), כולל שם/אימייל השואל וסטטוס."""
    limit = max(1, min(limit, 500))
    conn = get_conn(); cur = conn.cursor()
    where = "WHERE l.status <> 'ok'" if only_failed else ""
    cur.execute(f"""
        SELECT l.id, l.created_at, u.name, u.email, l.message, l.reply, l.topic, l.on_topic, l.status, l.error_text
        FROM ai_chat_log l LEFT JOIN users u ON u.id = l.user_id
        {where}
        ORDER BY l.id DESC LIMIT %s;
    """, (limit,))
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE status <> 'ok') FROM ai_chat_log;")
    total, failed = cur.fetchone()
    cur.close(); conn.close()
    return {
        "total": total, "failed": failed,
        "items": [
            {"id": r[0], "created_at": r[1].strftime("%d/%m/%Y %H:%M") if r[1] else "",
             "user_name": r[2], "user_email": r[3], "message": r[4], "reply": r[5],
             "topic": r[6], "on_topic": r[7], "status": r[8], "error_text": r[9]}
            for r in rows
        ],
    }


@app.post("/api/ai/chat")
def ai_chat(body: AiChatBody, sess: dict = Depends(require_auth)):
    user_message = (body.message or "").strip()[:AI_MAX_MSG_CHARS]
    if not user_message:
        return {"reply": "כתבו לי שאלה ואשמח לעזור 🙂", "ok": True}

    is_admin = sess.get("role") == "admin"

    fallback = (
        "מצטער/ת, העוזר החכם עדיין לא מחובר במלואו. בינתיים אפשר ליצור איתנו קשר "
        "ישירות בטלפון או בוואטסאפ ונשמח לעזור! 🧁"
    )

    # בדיקת חסימה/מכסה יומית — לא חלה על מנהלים בכלל
    if not is_admin:
        allowed, guard_msg = _ai_check_guard(sess["user_id"])
        if not allowed:
            _ai_log_chat(sess["user_id"], user_message, guard_msg, status="guarded")
            return {"reply": guard_msg, "ok": False, "reason": "guarded"}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _ai_log_chat(sess["user_id"], user_message, fallback, status="no_api_key",
                     error_text="ANTHROPIC_API_KEY לא מוגדר בשרת (Railway → Variables)")
        return {"reply": fallback, "ok": False, "reason": "no_api_key"}

    try:
        import anthropic
    except Exception as e:
        _ai_log_chat(sess["user_id"], user_message, fallback, status="package_missing",
                     error_text=f"חבילת anthropic לא מותקנת: {e}")
        return {"reply": fallback, "ok": False, "reason": "package_missing"}

    context = _ai_build_context(user_message)
    system = AI_SYSTEM_PROMPT + "\n\n===== נתוני החנות (לשימושך בלבד) =====\n" + context

    # בונים את שרשור השיחה (עד 8 הודעות אחרונות מההיסטוריה)
    messages = []
    if body.history:
        for m in body.history[-8:]:
            role = m.role if m.role in ("user", "assistant") else "user"
            content = (m.content or "").strip()[:AI_MAX_MSG_CHARS]
            if content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=700,
            system=system,
            messages=messages,
        )
        raw = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()

        # מנסים לפרש כ-JSON מובנה {"reply", "topic", "on_topic"}. אם המודל
        # (בעיקר מודלים חינמיים) לא הצליח לשמור על הפורמט — נופלים בעדינות
        # לטקסט הגולמי כתשובה רגילה, בלי לשבור את הצ'אט, ובלי סיווג/הגנה
        # לאותה הודעה בודדת (עדיף לפספס הגנה אחת מאשר לשבור את החוויה).
        reply, topic, on_topic = None, None, None
        try:
            import json, re
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            parsed = json.loads(cleaned)
            reply = (parsed.get("reply") or "").strip()
            topic = (parsed.get("topic") or "").strip().lower()
            on_topic = parsed.get("on_topic")
            if not isinstance(on_topic, bool):
                on_topic = None
        except Exception:
            reply = raw

        if not reply:
            reply = fallback
        _track_faq_topic(topic)
        _ai_log_chat(sess["user_id"], user_message, reply, topic=topic, on_topic=on_topic, status="ok")
        if not is_admin:
            _ai_record_usage_and_ontopic(sess["user_id"], on_topic)
        return {"reply": reply, "ok": True}
    except Exception as e:
        print(f"[AI] error: {e}")
        _ai_log_chat(sess["user_id"], user_message, fallback, status="api_error", error_text=f"{type(e).__name__}: {e}")
        return {"reply": fallback, "ok": False, "reason": "api_error"}


# --- הגשת קבצים סטטיים (HTML, לוגו, robots.txt) ---
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# מונע מהדפדפן (בעיקר בנייד) לשמור גרסה ישנה של ה-HTML במטמון.
# ככה, מיד אחרי כל פרסום, המבקרים רואים את הגרסה החדשה בלי צורך לנקות cache.
_NO_CACHE_HTML = {"Cache-Control": "no-cache, no-store, must-revalidate"}

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=_NO_CACHE_HTML)

@app.get("/admin.html")
def serve_admin():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"), headers=_NO_CACHE_HTML)

@app.get("/staff.html")
def serve_staff():
    return FileResponse(os.path.join(STATIC_DIR, "staff.html"), headers=_NO_CACHE_HTML)

@app.get("/logo.jpg")
def serve_logo():
    path = os.path.join(STATIC_DIR, "logo.jpg")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404)

@app.get("/robots.txt")
def serve_robots():
    path = os.path.join(STATIC_DIR, "robots.txt")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404)