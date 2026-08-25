from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from datetime import datetime, date, time
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

# --- CORS: בפרודקשן, הגדירי ALLOWED_ORIGINS כמשתנה סביבה עם הדומיין שלך ---
# לדוגמה: ALLOWED_ORIGINS=https://bakeparty.co.il,https://www.bakeparty.co.il
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- חיבור ל-DB: תומך במשתני סביבה (לפרודקשן) או ברירת מחדל (לפיתוח) ---
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5433"),
    "dbname": os.environ.get("DB_NAME", "bakeshop"),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "adminpassword"),
}

# --- יצירת טבלאות ושדות חדשים באופן אוטומטי ---
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

    # רשת ביטחון: אלו נוצרות רגיל דרך create_admin.py, אבל אם השרת עולה
    # לפני שהריצו אותו (למשל אחרי clone טרי של הריפו), עדיף שהאתר לא יקרוס
    # לגמרי במקום להציג שגיאת "relation does not exist" בכל טעינה.
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
    # --- טבלאות users + settings (נוצרות בדרך כלל ע"י create_admin.py) ---
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, name VARCHAR(150), email VARCHAR(255) UNIQUE NOT NULL,
            phone VARCHAR(50), address TEXT, password_hash TEXT NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'customer', created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    for col in ("phone", "address", "name"):
        init_cur.execute(f"ALTER TABLE users ALTER COLUMN {col} DROP NOT NULL;")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value TEXT);
    """)
    init_cur.execute("""
        INSERT INTO settings (key, value) VALUES ('opening_time','09:00'),('closing_time','18:00'),
        ('delivery_override','auto') ON CONFLICT (key) DO NOTHING;
    """)

    # --- יצירת מנהל אוטומטית מ-env vars (לפרודקשן) ---
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
            "ON CONFLICT (email) DO UPDATE SET password_hash=EXCLUDED.password_hash, role='admin';",
            (admin_name, admin_email.lower(), _hash))
        print(f"✅ מנהל '{admin_email}' נוצר/עודכן אוטומטית מ-env vars")

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

# --- Authentication ---
SESSIONS = {}
SESSION_TTL = 60 * 60 * 24 * 7

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

def _session_from_header(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1]
    sess = SESSIONS.get(token)
    if not sess or sess["expires"] < time_module.time():
        SESSIONS.pop(token, None)
        return None
    return sess

def require_auth(authorization: str = Header(None)):
    sess = _session_from_header(authorization)
    if not sess:
        raise HTTPException(status_code=401, detail="נדרשת התחברות")
    return sess

def require_admin(authorization: str = Header(None)):
    sess = _session_from_header(authorization)
    if not sess:
        raise HTTPException(status_code=401, detail="נדרשת התחברות")
    if sess["role"] != "admin":
        raise HTTPException(status_code=403, detail="נדרשות הרשאות מנהל")
    return sess

def _user_public(row):
    return {"id": row[0], "name": row[1], "email": row[2], "phone": row[3], "address": row[4], "role": row[5]}

def _fetch_user(cur, user_id):
    cur.execute("SELECT id, name, email, phone, address, role FROM users WHERE id = %s;", (user_id,))
    return cur.fetchone()

class RegisterBody(BaseModel):
    name: str
    email: str
    password: str
    phone: Optional[str] = None
    address: Optional[str] = None

class LoginBody(BaseModel):
    email: str
    password: str

def _issue_token(user_id, role):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user_id": user_id, "role": role, "expires": time_module.time() + SESSION_TTL}
    return token

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

# --- הגנת brute force: מקסימום 10 ניסיונות התחברות כושלים בדקה מכתובת IP ---
LOGIN_ATTEMPTS = defaultdict(list)  # ip -> [timestamps of failed attempts]

def check_rate_limit(ip: str, max_attempts: int = 10, window: int = 60):
    now = time_module.time()
    LOGIN_ATTEMPTS[ip] = [t for t in LOGIN_ATTEMPTS[ip] if now - t < window]
    if len(LOGIN_ATTEMPTS[ip]) >= max_attempts:
        raise HTTPException(status_code=429, detail="יותר מדי ניסיונות התחברות. נסו שוב בעוד דקה.")

@app.post("/api/login")
def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)
    email = body.email.strip().lower()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, password_hash, role FROM users WHERE email = %s;", (email,))
    row = cur.fetchone()
    if not row or not verify_password(body.password, row[1]):
        cur.close(); conn.close()
        LOGIN_ATTEMPTS[ip].append(time_module.time())
        raise HTTPException(status_code=401, detail="אימייל או סיסמה שגויים")
    user = _fetch_user(cur, row[0])
    cur.close(); conn.close()
    LOGIN_ATTEMPTS.pop(ip, None)  # איפוס אחרי הצלחה
    return {"token": _issue_token(row[0], row[2]), "user": _user_public(user)}

@app.post("/api/logout")
def logout(authorization: str = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        SESSIONS.pop(authorization.split(" ", 1)[1], None)
    return {"ok": True}

@app.get("/api/me")
def get_me(sess: dict = Depends(require_auth)):
    conn = get_conn(); cur = conn.cursor()
    user = _fetch_user(cur, sess["user_id"])
    cur.close(); conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="המשתמש לא נמצא")
    return _user_public(user)

# --- Store Hours & Deliveries ---
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

    now_dt = datetime.now()
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

    # שימו לב: אין כרגע מערכת הזמנות באתר, אז ההודעה כאן מתארת רק את מצב
    # החנות עצמה (פתוחה/סגורה) ולא רומזת על אפשרות "להזמין" משהו.
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

    today_date = datetime.now().date()
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
        # בדיקה בטוחה של המערך החוזר ממסד הנתונים
        cat_ids = list(p[10]) if len(p) > 10 and p[10] else []
        if p[6] and p[6] not in cat_ids:
            cat_ids.append(p[6])
            
        data.append({
            "id": p[0], "name": p[1], "price": float(p[2]), "in_stock": p[3] > 0, 
            "category": p[4], "image": p[5] if p[5] else PLACEHOLDER, "category_id": p[6],
            "weight_grams": p[7], "units_per_package": p[8], "notes": p[9],
            "category_ids": cat_ids
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

# --- פונקציית שליפת מוצרים משודרגת עם Subquery מאובטח ---
@app.get("/api/products")
def get_products():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url, 
               p.category_id, p.weight_grams, p.units_per_package, p.notes,
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id)
        FROM Products p 
        LEFT JOIN Categories c ON p.category_id = c.id 
        WHERE p.category_id IS NOT NULL 
        ORDER BY p.stock_quantity DESC, p.id;
    """)
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
               (SELECT array_agg(category_id) FROM product_categories WHERE product_id = p.id)
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

@app.patch("/api/admin/products/{product_id}")
def admin_update_product(product_id: int, body: ProductUpdateBody, sess: dict = Depends(require_admin)):
    fields = body.model_dump(exclude_unset=True)
    conn = get_conn(); cur = conn.cursor()
    
    cat_ids = fields.pop("category_ids", None)

    # תוקן: קודם ה-pop() מוציא את category_ids מ-fields, ואז הבדיקה "if fields"
    # הייתה נכנסת ל-False בדיוק כשהבקשה מכילה רק category_ids (המקרה של טאב
    # "מוצרים וקטגוריות" ב-admin.html, ששולח *רק* את זה) - כך שהעדכון של
    # category_id (הקטגוריה הראשית בטבלת Products) פשוט לא קרה בפועל אף פעם.
    # התוצאה: שינוי/הסרת קטגוריות בפאנל הניהול עדכן רק את טבלת הקישור
    # product_categories, בעוד שהעמודה category_id (שקובעת בפועל תחת איזו
    # קטגוריה המוצר מוצג באתר, ואם הוא "ללא קטגוריה") נשארה עם הערך הישן.
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

@app.post("/api/admin/products")
def admin_create_product(body: ProductCreateBody, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    primary_cat = body.category_ids[0] if body.category_ids else None
    
    cur.execute(
        """
        INSERT INTO Products (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id;
        """,
        (body.name.strip(), primary_cat, body.price, body.stock_quantity, body.weight_grams, body.units_per_package, body.image_url, body.notes)
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

@app.get("/api/products/{product_id}")
def get_product_detail(product_id: int):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        """
        SELECT p.id, p.name, p.price, p.stock_quantity, c.name, p.image_url,
               p.category_id, p.weight_grams, p.units_per_package, p.notes
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
        "category_ids": cats
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

# --- פרטי יצירת קשר (ניתנים לעריכה ע"י המנהל, נשמרים בטבלת settings) ---
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

# --- הגשת קבצים סטטיים (HTML, לוגו, robots.txt) ---
# בפרודקשן, FastAPI מגיש את כל הקבצים — לא צריך שרת נפרד.
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

@app.get("/admin.html")
def serve_admin():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))

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