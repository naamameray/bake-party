from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from datetime import datetime, date, time, timedelta, timezone
import json
import threading
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
    if request.url.scheme == "https":
        # Browsers remember to use HTTPS only for this site for a year
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if response.headers.get("content-type", "").startswith("text/html"):
        # Content-Security-Policy: the page may only load scripts, styles and frames from the listed places,
        # so injected markup can't pull in a foreign script or send data to a foreign server.
        header = "Content-Security-Policy-Report-Only" if CSP_REPORT_ONLY else "Content-Security-Policy"
        response.headers.setdefault(header, CSP_POLICY)
    return response

# Railway's own domain stays allowed, so the site keeps working if the custom domain has a problem
_RAILWAY_ORIGIN = "https://bake-party-production.up.railway.app"
CSP_POLICY = "; ".join([
    "default-src 'self'",
    # 'unsafe-inline': the pages use inline scripts and onclick handlers. Everything else is blocked.
    "script-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/client",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com/gsi/style",
    "font-src 'self' https://fonts.gstatic.com data:",
    # Product photos may come from Wolt's image servers and Google profile photos
    "img-src 'self' data: blob: https:",
    f"connect-src 'self' {_RAILWAY_ORIGIN} https://accounts.google.com/gsi/",
    "frame-src https://accounts.google.com/gsi/",
    "worker-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
])
# CSP_REPORT_ONLY=1 only reports violations in the browser console instead of blocking (a safe fallback)
CSP_REPORT_ONLY = os.environ.get("CSP_REPORT_ONLY", "").lower() in ("1", "true", "yes")

from fastapi.middleware.gzip import GZipMiddleware
# Compression: the product list drops from ~900KB to ~100KB, so the site loads much faster on phones
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Only our own sites may call the API from a browser. Override with ALLOWED_ORIGINS (comma separated).
_DEFAULT_ORIGINS = ["https://www.bakeparty.co.il", "https://bakeparty.co.il", _RAILWAY_ORIGIN,
                    "http://localhost:8000", "http://127.0.0.1:8000"]
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()] or _DEFAULT_ORIGINS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # Login uses an Authorization header, not cookies, so credentials mode isn't needed
    allow_credentials=False,
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
    # On an empty database (first deploy) the product tables don't exist yet. Without this the whole setup fails,
    # including users and hours. The schema matches tools/import_data.py.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS Categories (
            id SERIAL PRIMARY KEY, wolt_id VARCHAR(64) UNIQUE, name VARCHAR(255) NOT NULL,
            parent_id INTEGER REFERENCES Categories(id) ON DELETE SET NULL,
            image_url TEXT, sort_order INTEGER DEFAULT 0
        );
    """)
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS Products (
            id SERIAL PRIMARY KEY, name VARCHAR(500) NOT NULL,
            category_id INTEGER REFERENCES Categories(id) ON DELETE SET NULL,
            price NUMERIC(10,2) NOT NULL DEFAULT 0, stock_quantity INTEGER NOT NULL DEFAULT 0,
            weight_grams INTEGER, units_per_package INTEGER, image_url TEXT, notes TEXT
        );
    """)
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
    # Date added, for the "New in store" row. No default at creation time, so existing products
    # are not all marked as new. Only products added from now on get a date.
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

    # --- Roles and account security: customer | employee | admin ---
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_logins INTEGER NOT NULL DEFAULT 0;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMP;")
    # Sign in with Google: the Google account id and its profile picture (shown on reviews)
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub VARCHAR(64);")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;")
    init_cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_users_google_sub ON users(google_sub) WHERE google_sub IS NOT NULL;")
    # Two-factor authentication (authenticator app). totp_last_step blocks reusing the same code.
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret TEXT;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_pending TEXT;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT FALSE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_step BIGINT;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS backup_codes TEXT;")
    # Short-lived "password OK, waiting for the 2FA code" challenges
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS login_challenges (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            attempts   INTEGER NOT NULL DEFAULT 0,
            expires_at TIMESTAMPTZ NOT NULL
        );
    """)
    # Password reset links: only the token's hash is stored; single use; expire after 30 minutes
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at    TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("DELETE FROM password_resets WHERE expires_at < NOW() - INTERVAL '7 days';")
    init_cur.execute("DELETE FROM login_challenges WHERE expires_at < NOW();")
    # Sessions live in the database (not in memory), so a new deploy doesn't log anyone out.
    # Only the token's hash is stored, never the token itself.
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);")
    # Audit log: who did what and when. Written only by the server; it can't be edited or deleted from the site.
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

    # --- Work hours and pay ---
    # users.hourly_rate is the current rate. Each shift keeps a copy of the rate at clock-in,
    # so a later rate change doesn't alter past shifts. Only an admin changes rates or shifts.
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
    # One employee can't have two open shifts at once (also enforced by the database)
    init_cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_open_shift_per_user ON shifts(user_id) WHERE clock_out IS NULL;")

    # --- Web Push. Customers get messages only if notify_consent = TRUE (explicit opt-in) ---
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_consent BOOLEAN NOT NULL DEFAULT FALSE;")
    init_cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_consent_at TIMESTAMPTZ;")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint   TEXT NOT NULL UNIQUE,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);")

    # --- AI chat protection: temporary block after repeated off-topic messages + a daily limit ---
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

    # --- AI chat log: what was asked, what was answered, and by whom (visible to admins) ---
    # Failed attempts are stored too (with the reason), so problems can be diagnosed without digging through logs.
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
    # Store address fix: Yoni Netanyahu 25 (also when an old address, 21 or 23, was saved before)
    init_cur.execute("UPDATE settings SET value = regexp_replace(value, 'יוני נתניהו (21|23)\\M', 'יוני נתניהו 25') "
                     "WHERE key = 'contact_address' AND value ~ 'יוני נתניהו (21|23)\\M';")
    # The shop's new email address (replaces the old one if it was saved in the settings)
    init_cur.execute("UPDATE settings SET value = 'bakeparty2026@gmail.com' WHERE key = 'contact_email' AND lower(trim(value)) = 'pinukimmam@gmail.com';")

    # --- FAQ, including items that are "discovered" automatically from the chat ---
    # topic_key: a fixed label the AI assigns to every chat question (see _ai_build_context / ai_chat).
    # Once a topic has been asked FAQ_AUTO_PUBLISH_THRESHOLD times, it is published on the site automatically.
    # Questions and answers are written in advance (not raw chat text), to keep the wording accurate.
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
        # These three are not shown yet; they go live once they've been asked about enough times in the chat
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

    # --- Pickup orders ---
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id            SERIAL PRIMARY KEY,
            token         VARCHAR(64) NOT NULL,
            user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
            customer_name VARCHAR(150) NOT NULL,
            phone         VARCHAR(20) NOT NULL,
            email         VARCHAR(255),
            note          TEXT,
            status        VARCHAR(20) NOT NULL DEFAULT 'new',
            total         NUMERIC(10,2) NOT NULL DEFAULT 0,
            fulfillment   VARCHAR(20) NOT NULL DEFAULT 'pickup',
            handled_by    INTEGER,
            created_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW(),
            ready_at      TIMESTAMPTZ,
            picked_up_at  TIMESTAMPTZ
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, id);")
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, id DESC);")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id          SERIAL PRIMARY KEY,
            order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id  INTEGER,
            name        VARCHAR(500) NOT NULL,
            unit_price  NUMERIC(10,2) NOT NULL,
            quantity    INTEGER NOT NULL,
            missing     BOOLEAN NOT NULL DEFAULT FALSE,
            image_url   TEXT
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);")
    # Missing item: the customer decides what to do (remove = continue without it, replace = substitute + note). Staff-added items are flagged.
    init_cur.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS customer_choice VARCHAR(20);")
    init_cur.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS customer_note TEXT;")
    init_cur.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS added_by_staff BOOLEAN NOT NULL DEFAULT FALSE;")
    # Substitute the customer picked: points to the missing item it replaces
    init_cur.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS substitute_for INTEGER;")
    # Per-order push subscriptions: even a guest can ask to be notified when their order is ready (not marketing consent)
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS order_push_subs (
            id         SERIAL PRIMARY KEY,
            order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            endpoint   TEXT NOT NULL,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (order_id, endpoint)
        );
    """)
    # The site now takes orders: update the old FAQ answer ("no ordering on the site") unless it was edited by hand
    init_cur.execute("""UPDATE faq_items SET question = 'איך מזמינים? אפשר להזמין דרך האתר?',
        answer = 'כן! מוסיפים מוצרים לעגלה באתר ושולחים הזמנה לאיסוף עצמי מהחנות. כשההזמנה מוכנה נעדכן אתכם, והתשלום בחנות בזמן האיסוף. אפשר להזמין גם בטלפון, בוואטסאפ או בוולט.',
        updated_at = NOW() WHERE topic_key = 'ordering' AND answer LIKE 'כרגע אין באתר מערכת הזמנות%';""")

    # --- Reviews: shown only after admin approval. Replies are published immediately (admins can delete them) ---
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name        VARCHAR(80) NOT NULL,
            rating      SMALLINT NOT NULL CHECK (rating BETWEEN 0 AND 5),
            text        TEXT,
            status      VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ,
            reviewed_by INTEGER
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status, id DESC);")
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS review_replies (
            id         SERIAL PRIMARY KEY,
            review_id  INTEGER NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
            user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            name       VARCHAR(80) NOT NULL,
            text       TEXT NOT NULL,
            is_store   BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_review_replies_review ON review_replies(review_id, id);")
    init_cur.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS photo_url TEXT;")

    # Notification log: every email and push that was sent or failed, with the reason. Shown in the admin panel (Messages tab)
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS notification_log (
            id         SERIAL PRIMARY KEY,
            channel    VARCHAR(10) NOT NULL,
            recipient  TEXT,
            subject    TEXT,
            ok         BOOLEAN NOT NULL,
            error      TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_notification_log_id ON notification_log(id DESC);")
    init_cur.execute("DELETE FROM notification_log WHERE created_at < NOW() - INTERVAL '60 days';")

    # Wolt API connection: OAuth tokens per venue (secret, never exported) and incoming order notifications
    init_cur.execute("""
        CREATE TABLE IF NOT EXISTS wolt_connection (
            id            SERIAL PRIMARY KEY,
            venue_id      TEXT UNIQUE,
            access_token  TEXT,
            refresh_token TEXT,
            expires_at    TIMESTAMPTZ,
            extra         TEXT,
            connected_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at    TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS wolt_events (
            id          SERIAL PRIMARY KEY,
            order_id    TEXT,
            status      TEXT,
            venue_id    TEXT,
            payload     TEXT,
            verified    BOOLEAN NOT NULL DEFAULT FALSE,
            received_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    init_cur.execute("CREATE INDEX IF NOT EXISTS idx_wolt_events_id ON wolt_events(id DESC);")
    init_cur.execute("DELETE FROM wolt_events WHERE received_at < NOW() - INTERVAL '90 days';")

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
    """Returns the signed-in session, or None. Role and status are read from the database on every request,
    so a role change or a disabled account takes effect immediately."""
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
    """Signed in, even if a password change is still required (only for changing it, /api/me and logout)."""
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
            "must_change_password": bool(row[6]) if len(row) > 6 else False,
            "avatar": row[7] if len(row) > 7 else None, "google": bool(row[8]) if len(row) > 8 else False}

def _fetch_user(cur, user_id):
    cur.execute("SELECT id, name, email, phone, address, role, must_change_password, avatar_url, google_sub "
                "FROM users WHERE id = %s;", (user_id,))
    return cur.fetchone()

def _audit(actor, action, target_type=None, target_id=None, details=None):
    """Writes to the audit log. Never fails the action itself if logging fails."""
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
    notify: bool = False        # Explicit opt-in for notifications (email and phone)

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
    if body.notify:
        cur.execute("UPDATE users SET notify_consent = TRUE, notify_consent_at = NOW() WHERE id = %s;", (user_id,))
    conn.commit()
    user = _fetch_user(cur, user_id)
    cur.close(); conn.close()
    if body.notify:
        threading.Thread(target=_send_welcome_email, args=(email, body.name.strip(), user_id), daemon=True).start()
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
    cur.close(); conn.close()
    LOGIN_ATTEMPTS.pop(ip, None)
    return _finish_login(row[0])

# ===================== Two-factor authentication and password reset =====================
TOTP_STEP = 30
CHALLENGE_MINUTES = 5
RESET_MINUTES = 30
TWOFA_ATTEMPTS = defaultdict(list)
RESET_ATTEMPTS = defaultdict(list)

def _finish_login(user_id, extra=None):
    """Second step of every sign-in (password or Google): if the account has 2FA, return a challenge
    instead of a session. The client then sends the code to /api/login/2fa."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT totp_enabled FROM users WHERE id = %s;", (user_id,))
    has_2fa = bool(cur.fetchone()[0])
    if has_2fa:
        challenge = secrets.token_urlsafe(32)
        cur.execute("INSERT INTO login_challenges (token_hash, user_id, expires_at) VALUES (%s, %s, NOW() + (%s || ' minutes')::interval);",
                    (_token_hash(challenge), user_id, str(CHALLENGE_MINUTES)))
        conn.commit(); cur.close(); conn.close()
        return {"two_factor": True, "challenge": challenge, **(extra or {})}
    user = _fetch_user(cur, user_id)
    cur.close(); conn.close()
    return {"token": _issue_token(user_id), "user": _user_public(user), **(extra or {})}

def _b32_secret():
    import base64
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

def _totp_at(secret, step):
    import base64, struct
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"

def _totp_match(secret, code, last_step=None):
    """Returns the matching time step (allowing one step of clock drift) or None. A step already used is rejected."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    now = int(time_module.time() // TOTP_STEP)
    for step in (now - 1, now, now + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_totp_at(secret, step), code):
            return step
    return None

def _new_backup_codes():
    codes = [f"{secrets.randbelow(10**4):04d}-{secrets.randbelow(10**4):04d}" for _ in range(8)]
    return codes, json.dumps([hashlib.sha256(c.encode()).hexdigest() for c in codes])

def _qr_svg(text):
    """QR code as inline SVG (for scanning into an authenticator app)."""
    try:
        import qrcode, qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
        return img.to_string(encoding="unicode")
    except Exception:
        return None

class TwoFactorCodeBody(BaseModel):
    code: str

class TwoFactorLoginBody(BaseModel):
    challenge: str
    code: str

@app.post("/api/login/2fa")
def login_2fa(body: TwoFactorLoginBody, request: Request):
    """Second sign-in step: a 6-digit code from the authenticator app, or a one-time backup code."""
    ip = request.client.host if request.client else "unknown"
    _simple_rate_limit(TWOFA_ATTEMPTS, ip, 10, 300, "יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.")
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT user_id, attempts FROM login_challenges WHERE token_hash = %s AND expires_at > NOW() FOR UPDATE;",
                    (_token_hash(body.challenge or ""),))
        ch = cur.fetchone()
        if not ch or ch[1] >= 5:
            raise HTTPException(status_code=401, detail="פג תוקף הכניסה. צריך להתחבר שוב.")
        user_id = ch[0]
        cur.execute("SELECT totp_secret, totp_last_step, backup_codes, active FROM users WHERE id = %s FOR UPDATE;", (user_id,))
        secret, last_step, backup, active = cur.fetchone()
        if not active:
            raise HTTPException(status_code=403, detail="החשבון הושבת. יש לפנות למנהל.")
        step = _totp_match(secret, body.code, last_step)
        used_backup = False
        if step is None:
            typed = (body.code or "").strip()
            hashes = json.loads(backup or "[]")
            h = hashlib.sha256(typed.encode()).hexdigest()
            if typed and h in hashes:
                hashes.remove(h); used_backup = True
                cur.execute("UPDATE users SET backup_codes = %s WHERE id = %s;", (json.dumps(hashes), user_id))
            else:
                cur.execute("UPDATE login_challenges SET attempts = attempts + 1 WHERE token_hash = %s;", (_token_hash(body.challenge),))
                conn.commit()
                raise HTTPException(status_code=401, detail="הקוד לא נכון. בודקים את הקוד העדכני באפליקציה ומנסים שוב.")
        else:
            cur.execute("UPDATE users SET totp_last_step = %s WHERE id = %s;", (step, user_id))
        cur.execute("DELETE FROM login_challenges WHERE token_hash = %s;", (_token_hash(body.challenge),))
        conn.commit()
        user = _fetch_user(cur, user_id)
    finally:
        cur.close(); conn.close()
    if used_backup:
        _audit({"user_id": user_id, "name": user[1]}, "2fa_backup_code_used", "user", user_id)
    return {"token": _issue_token(user_id), "user": _user_public(user)}

@app.get("/api/2fa/status")
def twofa_status(sess: dict = Depends(require_auth)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT totp_enabled, backup_codes FROM users WHERE id = %s;", (sess["user_id"],))
    enabled, backup = cur.fetchone(); cur.close(); conn.close()
    return {"enabled": bool(enabled), "backup_codes_left": len(json.loads(backup or "[]")) if enabled else 0}

@app.post("/api/2fa/setup")
def twofa_setup(sess: dict = Depends(require_staff)):
    """Step 1: create a new secret and show it as a QR code. Nothing changes until the code is confirmed."""
    secret = _b32_secret()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET totp_pending = %s WHERE id = %s RETURNING email;", (secret, sess["user_id"]))
    email = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    from urllib.parse import quote
    uri = f"otpauth://totp/{quote('Bake & Party')}:{quote(email)}?secret={secret}&issuer={quote('Bake & Party')}&digits=6&period={TOTP_STEP}"
    return {"secret": secret, "otpauth": uri, "qr_svg": _qr_svg(uri)}

@app.post("/api/2fa/enable")
def twofa_enable(body: TwoFactorCodeBody, sess: dict = Depends(require_staff)):
    """Step 2: confirm with a code from the app. Returns one-time backup codes (shown once)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT totp_pending FROM users WHERE id = %s FOR UPDATE;", (sess["user_id"],))
    pending = cur.fetchone()[0]
    step = _totp_match(pending, body.code)
    if not pending or step is None:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="הקוד לא נכון. סורקים שוב את הקוד ומקלידים את 6 הספרות שמופיעות באפליקציה.")
    codes, hashed = _new_backup_codes()
    cur.execute("UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, totp_enabled = TRUE, totp_last_step = %s, "
                "backup_codes = %s WHERE id = %s;", (step, hashed, sess["user_id"]))
    _drop_sessions(cur, sess["user_id"], keep_token_hash=sess["token_hash"])
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "2fa_enabled", "user", sess["user_id"])
    return {"ok": True, "backup_codes": codes}

@app.post("/api/2fa/disable")
def twofa_disable(body: TwoFactorCodeBody, sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT totp_secret, totp_last_step, backup_codes FROM users WHERE id = %s FOR UPDATE;", (sess["user_id"],))
    secret, last_step, backup = cur.fetchone()
    ok = _totp_match(secret, body.code, last_step) is not None or \
         hashlib.sha256((body.code or "").strip().encode()).hexdigest() in json.loads(backup or "[]")
    if not ok:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="הקוד לא נכון")
    cur.execute("UPDATE users SET totp_enabled = FALSE, totp_secret = NULL, totp_pending = NULL, backup_codes = NULL, "
                "totp_last_step = NULL WHERE id = %s;", (sess["user_id"],))
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "2fa_disabled", "user", sess["user_id"])
    return {"ok": True}

class ForgotBody(BaseModel):
    email: str

class ResetBody(BaseModel):
    token: str
    password: str

@app.post("/api/password/forgot")
def password_forgot(body: ForgotBody, request: Request):
    """Emails a single-use reset link. Always answers the same way, so it can't reveal which emails are registered."""
    ip = request.client.host if request.client else "unknown"
    email = (body.email or "").strip().lower()
    _simple_rate_limit(RESET_ATTEMPTS, ip, 5, 900, "נשלחו הרבה בקשות. נסו שוב בעוד כמה דקות.")
    _simple_rate_limit(RESET_ATTEMPTS, "email:" + email, 3, 3600, "כבר שלחנו כמה קישורים למייל הזה. כדאי לבדוק גם בספאם.")
    answer = {"ok": True, "message": "אם המייל רשום אצלנו, שלחנו אליו קישור לבחירת סיסמה חדשה. הקישור תקף לחצי שעה."}
    if "@" not in email:
        return answer
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name FROM users WHERE email = %s AND active = TRUE;", (email,))
    u = cur.fetchone()
    if not u:
        cur.close(); conn.close()
        return answer
    token = secrets.token_urlsafe(32)
    cur.execute("INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (%s, %s, NOW() + (%s || ' minutes')::interval);",
                (_token_hash(token), u[0], str(RESET_MINUTES)))
    conn.commit(); cur.close(); conn.close()
    link = f"{SITE_URL}/#reset={token}"
    first = (u[1] or "").split()[0] if (u[1] or "").split() else ""
    threading.Thread(target=_send_order_email, args=(email, "בחירת סיסמה חדשה – Bake & Party",
        [f"שלום {first},", "ביקשת לבחור סיסמה חדשה לחשבון שלך.", "לחצו על הכפתור למטה. הקישור תקף לחצי שעה ואפשר להשתמש בו פעם אחת.",
         "אם לא ביקשת, אפשר להתעלם מהמייל הזה והסיסמה לא תשתנה."], link, "לבחירת סיסמה חדשה"), daemon=True).start()
    return answer

@app.post("/api/password/reset")
def password_reset(body: ResetBody, request: Request):
    ip = request.client.host if request.client else "unknown"
    _simple_rate_limit(RESET_ATTEMPTS, "reset:" + ip, 10, 900, "יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.")
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("""SELECT r.user_id, u.role, u.name FROM password_resets r JOIN users u ON u.id = r.user_id
                       WHERE r.token_hash = %s AND r.used_at IS NULL AND r.expires_at > NOW() AND u.active = TRUE FOR UPDATE OF r;""",
                    (_token_hash(body.token or ""),))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="הקישור כבר לא בתוקף. אפשר לבקש קישור חדש ב\"שכחתי סיסמה\".")
        user_id, role, name = row
        min_len = 8 if role in STAFF_ROLES else 6
        if len(body.password or "") < min_len:
            raise HTTPException(status_code=400, detail=f"הסיסמה צריכה להיות באורך {min_len} תווים לפחות")
        cur.execute("UPDATE users SET password_hash = %s, must_change_password = FALSE, failed_logins = 0, locked_until = NULL WHERE id = %s;",
                    (hash_password(body.password), user_id))
        cur.execute("UPDATE password_resets SET used_at = NOW() WHERE user_id = %s AND used_at IS NULL;", (user_id,))
        _drop_sessions(cur, user_id)          # sign out everywhere: whoever had the old password is out
        conn.commit()
    finally:
        cur.close(); conn.close()
    _audit({"user_id": user_id, "name": name}, "password_reset_by_email", "user", user_id)
    return _finish_login(user_id)

# ---------- Sign in with Google ----------
# The browser gets a signed ID token from Google; the server checks it with Google before trusting it.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_ATTEMPTS = defaultdict(list)

class GoogleAuthBody(BaseModel):
    credential: str

@app.get("/api/auth/config")
def auth_config():
    return {"google_client_id": GOOGLE_CLIENT_ID or None}

def _verify_google_token(credential):
    """Validates a Google ID token via Google's tokeninfo endpoint. Returns its claims or raises."""
    import urllib.request, urllib.parse, urllib.error
    base = os.environ.get("GOOGLE_TOKENINFO_URL", "https://oauth2.googleapis.com/tokeninfo")   # overridable for local tests
    url = base + "?" + urllib.parse.urlencode({"id_token": credential})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            claims = json.loads(r.read().decode())
    except urllib.error.HTTPError:
        raise HTTPException(status_code=401, detail="ההתחברות עם Google לא אושרה. נסו שוב.")
    except Exception:
        raise HTTPException(status_code=503, detail="אין כרגע חיבור ל-Google. נסו שוב בעוד רגע.")
    if claims.get("aud") != GOOGLE_CLIENT_ID or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(status_code=401, detail="ההתחברות עם Google לא אושרה.")
    if str(claims.get("email_verified")).lower() != "true" or not claims.get("email"):
        raise HTTPException(status_code=401, detail="כתובת המייל בחשבון Google לא מאומתת.")
    if int(claims.get("exp", 0)) < time_module.time():
        raise HTTPException(status_code=401, detail="פג תוקף ההתחברות. נסו שוב.")
    return claims

@app.post("/api/auth/google")
def auth_google(body: GoogleAuthBody, request: Request):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=400, detail="התחברות עם Google עוד לא הופעלה באתר")
    ip = request.client.host if request.client else "unknown"
    _simple_rate_limit(GOOGLE_ATTEMPTS, ip, 20, 600, "יותר מדי ניסיונות התחברות. נסו שוב בעוד כמה דקות.")
    claims = _verify_google_token(body.credential)
    sub, email = str(claims["sub"]), claims["email"].strip().lower()
    name = (claims.get("name") or "").strip() or email.split("@")[0]
    picture = claims.get("picture") if str(claims.get("picture", "")).startswith("https://") else None
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT id, active FROM users WHERE google_sub = %s;", (sub,))
        row = cur.fetchone()
        if not row:
            # Same email already registered: Google has verified the address, so link the accounts
            cur.execute("SELECT id, active FROM users WHERE email = %s;", (email,))
            row = cur.fetchone()
        if row and not row[1]:
            raise HTTPException(status_code=403, detail="החשבון הושבת. יש לפנות לחנות.")
        if row:
            user_id = row[0]
            cur.execute("UPDATE users SET google_sub = %s, avatar_url = %s, name = COALESCE(NULLIF(name, ''), %s), "
                        "last_login = NOW(), failed_logins = 0, locked_until = NULL WHERE id = %s;", (sub, picture, name, user_id))
            new_user = False
        else:
            # New customer. The random password is never shown, so this account signs in only through Google
            cur.execute("INSERT INTO users (name, email, password_hash, role, google_sub, avatar_url, last_login) "
                        "VALUES (%s, %s, %s, 'customer', %s, %s, NOW()) RETURNING id;",
                        (name[:150], email, hash_password(secrets.token_urlsafe(24)), sub, picture))
            user_id = cur.fetchone()[0]
            new_user = True
        conn.commit()
    finally:
        cur.close(); conn.close()
    return _finish_login(user_id, {"new_user": new_user})

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

# ===================== User and staff management (admins only) =====================
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
        # Customers are read-only here: their details, role and status can't be changed, so no customer is affected by mistake
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
    # There must always be at least one active admin
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
        _drop_sessions(cur, user_id)      # Role changed or account disabled: sign out immediately
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
    # Also turns off 2FA, so a staff member who lost their phone can set it up again
    cur.execute("UPDATE users SET password_hash = %s, must_change_password = TRUE, failed_logins = 0, locked_until = NULL, "
                "totp_enabled = FALSE, totp_secret = NULL, totp_pending = NULL, backup_codes = NULL, totp_last_step = NULL "
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

# ===================== Work hours and pay =====================
MAX_SHIFT_HOURS = 24

def _month_bounds(month: Optional[str]):
    """'YYYY-MM' (Israel time) -> (start of month, start of next month, the month string)."""
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

# ---------- Employee / admin: time clock and my hours ----------
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

# ---------- Admin: hours and pay for everyone ----------
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
        # Shifts recorded before a rate existed get it now. Shifts that already have a rate don't change.
        cur.execute("UPDATE shifts SET hourly_rate = %s WHERE user_id = %s AND hourly_rate IS NULL;", (rate, user_id))
        filled = cur.rowcount
        # A currently open shift that started under another rate is updated too, so it isn't paid at the old rate
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "rate_change", "user", user_id, {"hourly_rate": [old, rate], "filled_shifts": filled})
    return {"ok": True, "filled_shifts": filled}

def _parse_local_dt(val: Optional[str]):
    """'YYYY-MM-DDTHH:MM' in Israel time."""
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
    clock_out: Optional[str] = None      # Empty string = open shift
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
        -- תתי-קטגוריות, וגם קטגוריות ראשיות שאין להן תתי-קטגוריות (אחרת מוצר ששויך אליהן
        -- מאבד את הקטגוריה בעריכה ונעלם מהאתר)
        SELECT id, full_name FROM (
            SELECT sub.id, p.name || ' > ' || sub.name AS full_name, p.name AS k1, sub.name AS k2
            FROM Categories sub JOIN Categories p ON sub.parent_id = p.id
            UNION ALL
            SELECT c.id, c.name, c.name, ''
            FROM Categories c
            WHERE c.parent_id IS NULL AND NOT EXISTS (SELECT 1 FROM Categories ch WHERE ch.parent_id = c.id)
        ) t ORDER BY k1, k2;
        """
    )
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "name": r[1]} for r in rows]

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024

@app.post("/api/admin/upload-image")
async def upload_image(file: UploadFile = File(...), sess: dict = Depends(require_admin)):
    # Images only, up to 8MB. Any other file (e.g. HTML) would be served from our own origin and could be used for an attack.
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise HTTPException(status_code=400, detail="אפשר להעלות רק תמונה (jpg, png, webp, gif)")
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="התמונה גדולה מדי (עד 8MB)")
    if not _image_kind(data):
        # The extension alone isn't enough: check the file really is an image
        raise HTTPException(status_code=400, detail="הקובץ לא נראה כמו תמונה תקינה")
    try:
        filename = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(IMAGES_DIR, filename), "wb") as f:
            f.write(data)
        return {"url": f"/images/{filename}"}
    except Exception:
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
    """Recently added products (last 45 days), for the "New in store" row on the home page."""
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
    """Paginated product list for the admin panel, with search and filtering."""
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
    """Duplicates a product (image, price, categories, notes etc.) into a new one.
    Handy for products that come in many variants (e.g. food coloring in several colors):
    duplicate once, then change only the name/weight/color of each copy.
    The new name ends with "(עותק)" (copy) so it's easy to spot and edit.
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

    # Copy every extra category link too (the link table, not just the main category)
    cur.execute("SELECT category_id FROM product_categories WHERE product_id = %s;", (product_id,))
    for (cid,) in cur.fetchall():
        cur.execute(
            "INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (new_id, cid),
        )

    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "product_id": new_id}

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
    "contact_address": "יוני נתניהו 25, גבעת שמואל",
    "contact_email": "bakeparty2026@gmail.com",
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

# --- FAQ ---

@app.get("/api/faq")
def get_faq():
    """Published FAQ items only, for the home page."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, question, answer FROM faq_items WHERE published = TRUE ORDER BY sort_order, id;")
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "question": r[1], "answer": r[2]} for r in rows]

@app.get("/api/admin/faq")
def admin_list_faq(sess: dict = Depends(require_admin)):
    """All items, including unpublished ones, with the count of chat questions per topic."""
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

# ===================== Notifications (Web Push) =====================
# Principles:
#  - A customer is notified only after opting in (notify_consent) AND granting browser permission. Otherwise nothing is ever sent.
#  - Opting out deletes the subscriptions and resets the consent immediately.
#  - One combined message about new products (not one per product), at most every 12 hours, 09:00-20:00 only, never on Saturday.
# ---------- Email ----------
# Railway settings: SMTP_HOST, SMTP_PORT (465 or 587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (optional), SITE_URL (optional)
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
# Sending through Brevo (free up to 300 emails a day). It works over HTTPS instead of SMTP,
# so it also works where Railway blocks SMTP. When BREVO_API_KEY is set it is used instead of SMTP.
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
EMAIL_FROM = (os.environ.get("EMAIL_FROM", "") or SMTP_FROM).strip()
SITE_URL = os.environ.get("SITE_URL", "https://www.bakeparty.co.il").rstrip("/")

_LAST_EMAIL_ERROR = None      # Reason for the last failed send (no secrets), shown in the admin panel

def _log_notify(channel, recipient, subject, ok, error=None):
    """Writes to the notification log. Never fails the send itself."""
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("INSERT INTO notification_log (channel, recipient, subject, ok, error) VALUES (%s,%s,%s,%s,%s);",
                    (channel, (recipient or "")[:255], (subject or "")[:255], ok, (str(error)[:400] if error else None)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"[NOTIFY-LOG] {e}")

def _push_target(endpoint):
    host = (endpoint or "").split("/")[2] if (endpoint or "").count("/") >= 2 else ""
    return {"fcm.googleapis.com": "אנדרואיד/כרום", "web.push.apple.com": "אייפון/ספארי",
            "updates.push.services.mozilla.com": "פיירפוקס"}.get(host, host or "מכשיר")

def _email_mode():
    if BREVO_API_KEY and EMAIL_FROM:
        return "brevo"
    if SMTP_HOST and SMTP_USER and SMTP_PASSWORD:
        return "smtp"
    return None

def _email_configured():
    return _email_mode() is not None

EMAIL_NOT_CONFIGURED = "המייל לא מוגדר בשרת. צריך להוסיף ב-Railway את BREVO_API_KEY ו-EMAIL_FROM (או הגדרות SMTP)."

class _Mailer:
    """Opens one connection (SMTP) and sends several emails. With Brevo, each email is its own HTTPS request."""
    def __enter__(self):
        self.mode = _email_mode()
        self.srv = None
        if self.mode == "smtp":
            import smtplib, ssl
            if SMTP_PORT == 465:
                self.srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30)
            else:
                self.srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30); self.srv.starttls(context=ssl.create_default_context())
            self.srv.login(SMTP_USER, SMTP_PASSWORD)
        return self

    def send(self, to, subject, text, html, headers=None, attachments=None):
        """attachments: [(filename, bytes)]"""
        if self.mode == "brevo":
            import urllib.request, urllib.error, base64
            body = {"sender": {"name": "Bake & Party", "email": EMAIL_FROM}, "to": [{"email": to}],
                    "subject": subject, "textContent": text, "htmlContent": html}
            if headers:
                body["headers"] = headers
            if attachments:
                body["attachment"] = [{"name": n, "content": base64.b64encode(b).decode()} for n, b in attachments]
            req = urllib.request.Request(os.environ.get("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email"), data=json.dumps(body).encode(),
                                         headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=20).read()
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                raise RuntimeError(f"Brevo {e.code}: {detail}")
            return
        from email.message import EmailMessage
        from email.utils import formataddr
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("Bake & Party", SMTP_FROM))
        msg["To"] = to
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        for n, b in (attachments or []):
            msg.add_attachment(b, maintype="application", subtype="zip", filename=n)
        self.srv.send_message(msg)

    def __exit__(self, *a):
        if self.srv:
            try: self.srv.quit()
            except Exception: pass

def _unsub_secret():
    conn = get_conn(); cur = conn.cursor()
    sec = _setting_get(cur, "unsub_secret")
    if not sec:
        sec = secrets.token_hex(32); _setting_set(cur, "unsub_secret", sec); conn.commit()
    cur.close(); conn.close()
    return sec

def _unsub_token(user_id):
    return hmac.new(_unsub_secret().encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]

def _send_emails(recipients, subject, text, url="/", promo=True):
    """recipients: [(user_id, email, name)]. Every email has a personal unsubscribe link. Returns how many were sent."""
    global _LAST_EMAIL_ERROR
    if not _email_configured() or not recipients:
        return 0
    import time as _t
    subj = ("פרסומת | " + subject) if promo else subject        # Israeli Communications Law (section 30A): promotional messages must be labeled
    sent = 0
    try:
        mailer = _Mailer().__enter__()
    except Exception as e:
        _LAST_EMAIL_ERROR = f"חיבור לשרת המייל נכשל: {str(e)[:160]}"
        _log_notify("email", ", ".join(r[1] for r in recipients[:3]), subj, False, _LAST_EMAIL_ERROR)
        print(f"[EMAIL] connect failed: {e}"); return 0
    try:
        for uid, to, name in recipients:
            try:
                unsub = f"{SITE_URL}/unsubscribe?u={uid}&t={_unsub_token(uid)}"
                link = SITE_URL + (url if url.startswith("/") else "/")
                first = ((name or "").split() or [""])[0]
                hi = f"שלום {first}," if first else "שלום,"
                plain = f"{hi}\n\n{text}\n\nלצפייה באתר: {link}\n\nBake & Party, גבעת שמואל\nקיבלת הודעה זו כי אישרת קבלת עדכונים. להסרה: {unsub}"
                html = (f'<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#4A2E35">'
                        f'<h2 style="color:#E05276">Bake &amp; Party 🎉</h2><p>{hi}</p><p style="font-size:16px;line-height:1.6">{_html_escape(text)}</p>'
                        f'<p><a href="{link}" style="background:#FF85A2;color:#fff;padding:10px 20px;border-radius:18px;text-decoration:none;font-weight:bold">לצפייה באתר</a></p>'
                        f'<hr style="border:none;border-top:1px solid #FFD1DC"><p style="font-size:12px;color:#9B7B82">קיבלת הודעה זו כי אישרת קבלת עדכונים מ-Bake &amp; Party, גבעת שמואל. '
                        f'<a href="{unsub}" style="color:#9B7B82">להסרה מרשימת התפוצה</a></p></div>')
                mailer.send(to, subj, plain, html, {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
                sent += 1; _LAST_EMAIL_ERROR = None
                _log_notify("email", to, subj, True)
                _t.sleep(0.15)
            except Exception as e:
                _LAST_EMAIL_ERROR = f"שליחה נכשלה: {str(e)[:160]}"
                _log_notify("email", to, subj, False, _LAST_EMAIL_ERROR)
                print(f"[EMAIL] failed for user {uid}: {e}")
    finally:
        mailer.__exit__()
    return sent

def _html_escape(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

def _consenting_customer_emails():
    conn = get_conn(); cur = conn.cursor()
    # Customers only. Staff never receive customer promotions
    cur.execute("SELECT id, email, name FROM users WHERE notify_consent = TRUE AND active = TRUE AND email IS NOT NULL AND role = 'customer';")
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows

def notify_customers(title, body, url="/", sync_limit=25):
    """Message to opted-in customers: push + email. Up to sync_limit recipients are emailed immediately with a real result;
    above that, in the background. Returns (devices, email recipients, emails actually sent or None if in background)."""
    pushed = push_to_consenting_customers(title, body, url)
    recips = _consenting_customer_emails() if _email_configured() else []
    sent = None
    if recips:
        if len(recips) <= sync_limit:
            sent = _send_emails(recips, title, body, url)
        else:
            threading.Thread(target=_send_emails, args=(recips, title, body, url), daemon=True).start()
    return pushed, len(recips), sent

WELCOME_TEXT = "תודה שהצטרפת! מעכשיו נעדכן אותך על מוצרים חדשים ועל חדשות מהחנות. אפשר לבטל בכל רגע."

def _send_welcome_email(email, name, user_id):
    _send_emails([(user_id, email, name)], "תודה שהצטרפת!", WELCOME_TEXT, "/", promo=False)

VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "https://www.bakeparty.co.il")
PUSH_MIN_INTERVAL_HOURS = 12

def _push_configured():
    return bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)

def _send_push_rows(rows, title, body, url="/", table="push_subscriptions"):
    """rows: [(sub_id, endpoint, p256dh, auth)]. Sends, and deletes subscriptions that are no longer valid. Returns how many were sent."""
    if not rows:
        return 0
    if not _push_configured():
        _log_notify("push", f"{len(rows)} מכשירים", title, False, "התראות לטלפון לא מוגדרות בשרת (חסרים VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY)")
        return 0
    from pywebpush import webpush, WebPushException
    payload = json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False)
    sent, dead = 0, []
    for sub_id, endpoint, p256dh, auth in rows:
        try:
            webpush(subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                    data=payload, vapid_private_key=VAPID_PRIVATE_KEY, vapid_claims={"sub": VAPID_SUBJECT}, ttl=60 * 60 * 24)
            sent += 1
            _log_notify("push", _push_target(endpoint), title, True)
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(sub_id)
                _log_notify("push", _push_target(endpoint), title, False, "המכשיר כבר לא רשום להתראות (בוטל בדפדפן). צריך להפעיל מחדש.")
            else:
                _log_notify("push", _push_target(endpoint), title, False, f"שגיאה {code}: {str(e)[:200]}")
                print(f"[PUSH] failed ({code}): {e}")
        except Exception as e:
            _log_notify("push", _push_target(endpoint), title, False, str(e)[:200])
            print(f"[PUSH] error: {e}")
    if dead:
        conn = get_conn(); cur = conn.cursor()
        cur.execute(f"DELETE FROM {table} WHERE id = ANY(%s);", (dead,))
        conn.commit(); cur.close(); conn.close()
    return sent

def push_to_consenting_customers(title, body, url="/"):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps
                   JOIN users u ON u.id = ps.user_id
                   WHERE u.notify_consent = TRUE AND u.active = TRUE AND u.role = 'customer';""")
    rows = cur.fetchall(); cur.close(); conn.close()
    return _send_push_rows(rows, title, body, url)

def push_to_admins(title, body, url="/admin.html"):
    """Active admins only (e.g. a review waiting for approval, which only an admin can approve)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps
                   JOIN users u ON u.id = ps.user_id
                   WHERE u.role = 'admin' AND u.active = TRUE;""")
    rows = cur.fetchall(); cur.close(); conn.close()
    return _send_push_rows(rows, title, body, url)

def push_to_staff(title, body, url="/staff.html"):
    """Active staff (employees and admins) who enabled notifications on a device. Used for new orders."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""SELECT ps.id, ps.endpoint, ps.p256dh, ps.auth FROM push_subscriptions ps
                   JOIN users u ON u.id = ps.user_id
                   WHERE u.role IN ('employee', 'admin') AND u.active = TRUE;""")
    rows = cur.fetchall(); cur.close(); conn.close()
    if not rows:
        _log_notify("push", "צוות", title, False, "אף מכשיר של הצוות לא הפעיל התראות (במסך הצוות: \"התראות בטלפון על הזמנות\")")
    return _send_push_rows(rows, title, body, url)

def _setting_get(cur, key):
    cur.execute("SELECT value FROM settings WHERE key = %s;", (key,)); r = cur.fetchone()
    return r[0] if r else None

def _setting_set(cur, key, val):
    cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;", (key, val))

def run_new_products_digest(now=None):
    """Checks for new products not announced yet and, if any, sends one combined message. Returns a result dict (for tests)."""
    now = now or datetime.now(ISRAEL_TZ)
    if not (_push_configured() or _email_configured()):
        return {"skipped": "not_configured"}
    if not (9 <= now.hour < 20) or now.weekday() == 5:      # weekday 5 = Saturday
        return {"skipped": "quiet_hours"}
    conn = get_conn(); cur = conn.cursor()
    cursor_s = _setting_get(cur, "push_new_cursor"); last_s = _setting_get(cur, "push_new_last_sent")
    if cursor_s is None:                                    # First run: don't announce products that already exist
        cur.execute("SELECT NOW()::timestamp;")
        _setting_set(cur, "push_new_cursor", cur.fetchone()[0].isoformat())
        conn.commit(); cur.close(); conn.close()
        return {"skipped": "initialized"}
    if last_s and (datetime.now(timezone.utc) - datetime.fromisoformat(last_s)).total_seconds() < PUSH_MIN_INTERVAL_HOURS * 3600:
        cur.close(); conn.close(); return {"skipped": "too_soon"}
    # New, in-stock products. A 30-minute delay so a long editing session becomes a single message
    cur.execute("""SELECT COUNT(*), MAX(created_at) FROM Products
                   WHERE created_at IS NOT NULL AND stock_quantity > 0
                     AND created_at > %s::timestamp
                     AND created_at <= NOW()::timestamp - INTERVAL '30 minutes';""", (cursor_s,))
    count, newest = cur.fetchone()
    if not count:
        cur.close(); conn.close(); return {"skipped": "nothing_new"}
    conn.close()
    title = "Bake & Party 🎉"
    body = ("עלה מוצר חדש בחנות! כדאי להציץ ב\"חדש בחנות\"" if count == 1
            else "איזה כיף, עלו מוצרים חדשים! כדאי להציץ ב\"חדש בחנות\"")
    sent, emails, _mailed = notify_customers(title, body, "/#new-section")
    conn = get_conn(); cur = conn.cursor()
    _setting_set(cur, "push_new_cursor", newest.isoformat())
    _setting_set(cur, "push_new_last_sent", datetime.now(timezone.utc).isoformat())
    conn.commit(); cur.close(); conn.close()
    return {"sent": sent, "emails": emails, "new_products": count}

async def _digest_loop():
    import asyncio
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_new_products_digest)
        except Exception as e:
            print(f"[PUSH] digest error: {e}")
        await asyncio.sleep(600)

@app.on_event("startup")
async def _start_background_jobs():
    import asyncio
    asyncio.create_task(_digest_loop())
    asyncio.create_task(_backup_loop())

class PushSubBody(BaseModel):
    endpoint: str
    keys: dict

@app.get("/api/notifications/status")
def notifications_status(sess: dict = Depends(require_auth)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT notify_consent FROM users WHERE id = %s;", (sess["user_id"],))
    consent = bool(cur.fetchone()[0])
    cur.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id = %s;", (sess["user_id"],))
    n = cur.fetchone()[0]; cur.close(); conn.close()
    return {"configured": _push_configured(), "email_configured": _email_configured(), "any_channel": _push_configured() or _email_configured(),
            "public_key": VAPID_PUBLIC_KEY, "consent": consent, "devices": n}

@app.post("/api/notifications/consent")
def notifications_consent(sess: dict = Depends(require_auth)):
    """Opt in to notifications (email, plus push if the device subscribes). The welcome email is sent once."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE users SET notify_consent = TRUE, notify_consent_at = NOW() WHERE id = %s AND notify_consent = FALSE RETURNING name, email;", (sess["user_id"],))
    first = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if first and first[1]:
        threading.Thread(target=_send_welcome_email, args=(first[1], first[0], sess["user_id"]), daemon=True).start()
    return {"ok": True, "welcome_sent": bool(first)}

@app.post("/api/notifications/subscribe")
def notifications_subscribe(body: PushSubBody, sess: dict = Depends(require_auth)):
    p256dh, auth = body.keys.get("p256dh"), body.keys.get("auth")
    if not (body.endpoint.startswith("https://") and p256dh and auth):
        raise HTTPException(status_code=400, detail="נתוני מנוי לא תקינים")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (endpoint) DO UPDATE SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
                   RETURNING id, (xmax = 0);""", (sess["user_id"], body.endpoint, p256dh, auth))
    sub_id, inserted = cur.fetchone()
    if sess["role"] in STAFF_ROLES:
        # A staff device: subscribed for work alerts (orders) only, without marketing consent or a welcome email
        conn.commit(); cur.close(); conn.close()
        if inserted:
            _send_push_rows([(sub_id, body.endpoint, p256dh, auth)], "התראות על הזמנות הופעלו ✅",
                            "מעכשיו המכשיר הזה יקבל התראה על כל הזמנה חדשה באתר.", "/staff.html")
        return {"ok": True, "welcome_sent": bool(inserted)}
    cur.execute("UPDATE users SET notify_consent = TRUE, notify_consent_at = NOW() WHERE id = %s AND notify_consent = FALSE RETURNING name, email;", (sess["user_id"],))
    first = cur.fetchone()
    conn.commit(); cur.close(); conn.close()
    if first and first[1]:
        threading.Thread(target=_send_welcome_email, args=(first[1], first[0], sess["user_id"]), daemon=True).start()
    if inserted:      # Welcome push: once per new device
        name = (first[0] if first else None)
        hello = "תודה שהצטרפת! 🎉"
        _send_push_rows([(sub_id, body.endpoint, p256dh, auth)], hello,
                        "מעכשיו נעדכן אותך על מוצרים חדשים ועל חדשות מהחנות. אפשר לבטל בכל רגע.", "/")
    return {"ok": True, "welcome_sent": bool(inserted)}

@app.post("/api/notifications/unsubscribe")
def notifications_unsubscribe(sess: dict = Depends(require_auth)):
    """Full opt-out: deletes all of the user's devices and resets the consent."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM push_subscriptions WHERE user_id = %s;", (sess["user_id"],))
    cur.execute("UPDATE users SET notify_consent = FALSE WHERE id = %s;", (sess["user_id"],))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

from fastapi.responses import HTMLResponse

@app.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe_link(u: int, t: str):
    """Unsubscribe link from an email, no sign-in needed. Resets consent and deletes devices."""
    ok = hmac.compare_digest(_unsub_token(u), t)
    if ok:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("UPDATE users SET notify_consent = FALSE WHERE id = %s;", (u,))
        cur.execute("DELETE FROM push_subscriptions WHERE user_id = %s;", (u,))
        conn.commit(); cur.close(); conn.close()
    msg = "הוסרת בהצלחה מרשימת התפוצה. לא נשלח לך עוד הודעות." if ok else "הקישור לא תקין."
    return HTMLResponse(f'<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>הסרה מרשימת תפוצה</title><body style="font-family:Arial,sans-serif;text-align:center;padding:60px 20px;color:#4A2E35;background:#FFF9F5">'
                        f'<h2 style="color:#E05276">Bake &amp; Party</h2><p style="font-size:18px">{msg}</p><p><a href="/" style="color:#E05276">חזרה לאתר</a></p></body></html>')

@app.get("/api/admin/push-stats")
def admin_push_stats(sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE notify_consent = TRUE AND role = 'customer';"); consenting = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM push_subscriptions;"); devices = cur.fetchone()[0]
    last = _setting_get(cur, "push_new_last_sent"); cur.close(); conn.close()
    return {"configured": _push_configured(), "email_configured": _email_configured(), "email_mode": _email_mode(), "email_from": EMAIL_FROM if _email_mode() == "brevo" else SMTP_FROM, "last_email_error": _LAST_EMAIL_ERROR, "consenting_users": consenting, "devices": devices, "last_new_products_push": last}

@app.get("/api/admin/notification-log")
def admin_notification_log(limit: int = 50, sess: dict = Depends(require_admin)):
    limit = max(1, min(limit, 200))
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, channel, recipient, subject, ok, error, created_at FROM notification_log ORDER BY id DESC LIMIT %s;", (limit,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "channel": r[1], "recipient": r[2], "subject": r[3], "ok": r[4], "error": r[5] or "",
             "at": _il(r[6]).strftime("%d/%m %H:%M") if r[6] else ""} for r in rows]

class PushSendBody(BaseModel):
    title: str
    body: str
    url: str = "/"
    audience: str = "customers"      # customers | staff
    test_only: bool = False          # Send only to the admin's own devices

@app.post("/api/admin/push-send")
def admin_push_send(b: PushSendBody, sess: dict = Depends(require_admin)):
    title, body = b.title.strip(), b.body.strip()
    if not title or not body or len(title) > 60 or len(body) > 200:
        raise HTTPException(status_code=400, detail="צריך כותרת (עד 60 תווים) והודעה (עד 200 תווים)")
    if not b.url.startswith("/") or b.url.startswith("//") or b.audience not in ("customers", "staff"):
        raise HTTPException(status_code=400, detail="פרמטר לא תקין")
    if not (_push_configured() or _email_configured()):
        raise HTTPException(status_code=400, detail="ההתראות עוד לא הופעלו בשרת (חסרים מפתחות VAPID, או BREVO_API_KEY ו-EMAIL_FROM למייל, ב-Railway).")
    if b.test_only:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s;", (sess["user_id"],))
        rows = cur.fetchall()
        cur.execute("SELECT email, name FROM users WHERE id = %s;", (sess["user_id"],))
        me = cur.fetchone(); cur.close(); conn.close()
        pushed = _send_push_rows(rows, title, body, b.url) if rows else 0
        mailed = _send_emails([(sess["user_id"], me[0], me[1])], title, body, b.url) if (_email_configured() and me and me[0]) else 0
        if not pushed and not mailed:
            why = (_LAST_EMAIL_ERROR if _email_configured() and _LAST_EMAIL_ERROR else
                   "אין מכשיר רשום להתראות ולא הוגדר מייל. אפשר להירשם להתראות באתר הראשי (כפתור \"אישור קבלת התראות\").")
            raise HTTPException(status_code=400, detail="לא נשלח כלום אלייך. סיבה: " + why)
        email_error = None
        if not mailed:
            if not _email_configured():
                email_error = EMAIL_NOT_CONFIGURED
            elif not (me and me[0]):
                email_error = "לחשבון שלך אין כתובת מייל."
            else:
                email_error = _LAST_EMAIL_ERROR or "השליחה נכשלה בלי סיבה ידועה."
        return {"sent": pushed, "emails": mailed, "email_error": email_error,
                "email_to": me[0] if me else None, "email_from": (EMAIL_FROM if _email_mode() == "brevo" else SMTP_FROM) or None, "test": True}
    # Guard against accidental double sends: one audience message every 5 minutes
    conn = get_conn(); cur = conn.cursor()
    last = _setting_get(cur, "push_manual_last")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 300:
        cur.close(); conn.close()
        raise HTTPException(status_code=429, detail="נשלחה הודעה לפני פחות מ-5 דקות. כדי לא להציף, כדאי להמתין.")
    _setting_set(cur, "push_manual_last", datetime.now(timezone.utc).isoformat())
    conn.commit(); cur.close(); conn.close()
    mailed = None
    if b.audience == "customers":
        sent, emails, mailed = notify_customers(title, body, b.url)
    else:
        sent, emails = push_to_staff(title, body, b.url), 0
    _audit(sess, "push_send", "push", None, {"audience": b.audience, "title": title, "sent": sent, "emails": emails, "emails_sent": mailed})
    return {"sent": sent, "emails": emails, "emails_sent": mailed, "email_error": _LAST_EMAIL_ERROR if (emails and mailed is not None and mailed < emails) else None, "test": False}

# ===================== Pickup orders =====================
# Principles:
#  - Anyone can order (name and phone). A signed-in user is linked to the order; signing up during checkout is possible.
#  - Prices come from the database (never from the browser) and are stored on the order as they were at that moment.
#  - Payment happens in the store at pickup. No minimum order.
#  - Statuses: new -> preparing -> ready -> picked_up, or cancelled.
#  - When the order is ready the customer gets an email (if given) and a push (if subscribed), and there is a tracking page.

ORDER_STATUSES = ("new", "preparing", "ready", "picked_up", "cancelled")
ORDER_STATUS_LABELS = {"new": "התקבלה", "preparing": "בהכנה", "ready": "מוכנה לאיסוף", "picked_up": "נאספה", "cancelled": "בוטלה"}
ORDER_ACTIVE = ("new", "preparing", "ready")
ORDER_MAX_ITEMS = 60
ORDER_MAX_QTY = 99
ORDER_ATTEMPTS = defaultdict(list)

def _order_rate_limit(ip):
    now = time_module.time()
    ORDER_ATTEMPTS[ip] = [t for t in ORDER_ATTEMPTS[ip] if now - t < 600]
    if len(ORDER_ATTEMPTS[ip]) >= 6:
        raise HTTPException(status_code=429, detail="נשלחו הרבה הזמנות מהמכשיר הזה בזמן קצר. נסו שוב בעוד כמה דקות, או התקשרו אלינו.")
    ORDER_ATTEMPTS[ip].append(now)

def _norm_phone(p):
    digits = "".join(ch for ch in (p or "") if ch.isdigit())
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    return digits if 9 <= len(digits) <= 10 and digits.startswith("0") else None

def _orders_open(cur):
    return (_setting_get(cur, "orders_open") or "on") == "on"

def _order_number(order_id):
    return 1000 + order_id

def _send_order_email(to, subject, lines, link, button="למעקב אחרי ההזמנה", attachments=None):
    """Transactional order email (not marketing): order received, order ready, etc."""
    global _LAST_EMAIL_ERROR
    if not to:
        return False
    if not _email_configured():
        _log_notify("email", to, subject, False, EMAIL_NOT_CONFIGURED)
        return False
    try:
        text = "\n".join(lines)
        plain = f"{text}\n\n{button}: {link}\n\nBake & Party, {_get_contact()['address']}"
        body_html = "".join(f'<p style="margin:0 0 8px">{_html_escape(l)}</p>' for l in lines)
        html = (f'<div dir="rtl" style="font-family:Arial,sans-serif;max-width:520px;margin:auto;color:#4A2E35">'
                f'<h2 style="color:#E05276">Bake &amp; Party 🎉</h2>{body_html}'
                f'<p><a href="{link}" style="background:#E05276;color:#fff;padding:10px 20px;border-radius:18px;text-decoration:none;font-weight:bold">{_html_escape(button)}</a></p>'
                f'<hr style="border:none;border-top:1px solid #FFD1DC"><p style="font-size:12px;color:#9B7B82">Bake &amp; Party · {_html_escape(_get_contact()["address"])} · {_html_escape(_get_contact()["phone"])}</p></div>')
        with _Mailer() as m:
            m.send(to, subject, plain, html, attachments=attachments)
        _log_notify("email", to, subject, True)
        return True
    except Exception as e:
        _LAST_EMAIL_ERROR = f"שליחת מייל הזמנה נכשלה: {str(e)[:160]}"
        _log_notify("email", to, subject, False, _LAST_EMAIL_ERROR)
        print(f"[ORDER-EMAIL] failed: {e}")
        return False

def _push_to_user(user_id, title, body, url="/"):
    if not user_id:
        return 0
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s;", (user_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return _send_push_rows(rows, title, body, url)

def _push_order(order_id, user_id, title, body, url="/"):
    """Order notification: to devices subscribed to this order, and to the customer's devices if registered (no duplicates)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, endpoint, p256dh, auth FROM order_push_subs WHERE order_id = %s;", (order_id,))
    order_rows = cur.fetchall(); cur.close(); conn.close()
    sent = _send_push_rows(order_rows, title, body, url, table="order_push_subs")
    if user_id:
        seen = {r[1] for r in order_rows}
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s;", (user_id,))
        rows = [r for r in cur.fetchall() if r[1] not in seen]; cur.close(); conn.close()
        sent += _send_push_rows(rows, title, body, url)
    return sent

def _track_link(order_id, token):
    return f"{SITE_URL}/#order={order_id}&t={token}"

def _order_dict(cur, row, with_items=True, staff=False):
    (oid, token, user_id, name, phone, email, note, status, total, created, updated, ready_at, picked_at) = row
    d = {"id": oid, "number": _order_number(oid), "status": status, "status_label": ORDER_STATUS_LABELS.get(status, status),
         "name": name, "total": float(total), "note": note or "",
         "created_at": _il(created).strftime("%d/%m %H:%M") if created else "",
         "created_iso": _il(created).isoformat() if created else None,
         "ready_at": _il(ready_at).strftime("%d/%m %H:%M") if ready_at else None,
         "picked_up_at": _il(picked_at).strftime("%d/%m %H:%M") if picked_at else None}
    if staff:
        d.update({"phone": phone, "email": email or "", "registered": bool(user_id), "track_url": _track_link(oid, token)})
    if with_items:
        cur.execute("SELECT id, product_id, name, unit_price, quantity, missing, image_url, customer_choice, customer_note, added_by_staff, substitute_for "
                    "FROM order_items WHERE order_id = %s ORDER BY id;", (oid,))
        d["items"] = [{"id": r[0], "product_id": r[1], "name": r[2], "unit_price": float(r[3]), "quantity": r[4],
                       "line_total": round(float(r[3]) * r[4], 2), "missing": bool(r[5]), "image": r[6] or PLACEHOLDER,
                       "choice": r[7], "choice_note": r[8] or "", "added_by_staff": bool(r[9]), "substitute_for": r[10]}
                      for r in cur.fetchall()]
        d["awaiting_customer"] = status in ORDER_ACTIVE and any(i["missing"] and not i["choice"] for i in d["items"])
    return d

_ORDER_COLS = "id, token, user_id, customer_name, phone, email, note, status, total, created_at, updated_at, ready_at, picked_up_at"

def _recalc_total(cur, order_id):
    cur.execute("UPDATE orders SET total = COALESCE((SELECT SUM(unit_price * quantity) FROM order_items "
                "WHERE order_id = %s AND NOT missing), 0), updated_at = NOW() WHERE id = %s;", (order_id, order_id))

class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = 1

class OrderCreateBody(BaseModel):
    items: List[OrderItemIn]
    name: str
    phone: str
    email: Optional[str] = None
    note: Optional[str] = None
    create_account: bool = False
    password: Optional[str] = None
    notify: bool = False

@app.get("/api/orders/config")
def orders_config():
    conn = get_conn(); cur = conn.cursor()
    open_ = _orders_open(cur); cur.close(); conn.close()
    return {"open": open_, "pickup_only": True, "payment": "בתשלום בחנות באיסוף", "address": _get_contact()["address"],
            "push": _push_configured(), "public_key": VAPID_PUBLIC_KEY}

@app.post("/api/orders/{order_id}/push")
def order_push_subscribe(order_id: int, t: str, body: PushSubBody):
    """Subscribes this device to notifications for this order only (works for guests too)."""
    p256dh, auth = body.keys.get("p256dh"), body.keys.get("auth")
    if not (body.endpoint.startswith("https://") and p256dh and auth):
        raise HTTPException(status_code=400, detail="נתוני מנוי לא תקינים")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT token FROM orders WHERE id = %s;", (order_id,))
    o = cur.fetchone()
    if not o or not hmac.compare_digest(o[0], t or ""):
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="ההזמנה לא נמצאה")
    cur.execute("INSERT INTO order_push_subs (order_id, endpoint, p256dh, auth) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (order_id, endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth RETURNING id;",
                (order_id, body.endpoint, p256dh, auth))
    sub_id = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    _send_push_rows([(sub_id, body.endpoint, p256dh, auth)], f"מעולה! 🎉", f"נשלח לך התראה כשהזמנה #{_order_number(order_id)} מוכנה.",
                    f"/#order={order_id}&t={t}", table="order_push_subs")
    return {"ok": True}

@app.post("/api/orders")
def create_order(body: OrderCreateBody, request: Request, authorization: str = Header(None)):
    ip = request.client.host if request.client else "unknown"
    sess = _session_from_header(authorization)
    name = (body.name or "").strip()[:150]
    phone = _norm_phone(body.phone)
    email = (body.email or "").strip().lower()[:255] or None
    if not name:
        raise HTTPException(status_code=400, detail="צריך שם כדי שנדע של מי ההזמנה")
    if not phone:
        raise HTTPException(status_code=400, detail="מספר הטלפון לא נראה תקין")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(status_code=400, detail="כתובת המייל לא תקינה")
    qty = {}
    for it in body.items:
        if it.quantity < 1 or it.quantity > ORDER_MAX_QTY:
            raise HTTPException(status_code=400, detail="כמות לא תקינה")
        qty[it.product_id] = min(ORDER_MAX_QTY, qty.get(it.product_id, 0) + it.quantity)
    if not qty:
        raise HTTPException(status_code=400, detail="העגלה ריקה")
    if len(qty) > ORDER_MAX_ITEMS:
        raise HTTPException(status_code=400, detail="יותר מדי מוצרים שונים בהזמנה אחת")
    if body.create_account and not sess:
        if not email:
            raise HTTPException(status_code=400, detail="כדי ליצור חשבון צריך מייל")
        if not body.password or len(body.password) < 6:
            raise HTTPException(status_code=400, detail="הסיסמה חייבת להיות באורך 6 תווים לפחות")
    _order_rate_limit(ip)

    conn = get_conn(); cur = conn.cursor()
    try:
        if not _orders_open(cur):
            raise HTTPException(status_code=409, detail="כרגע לא מקבלים הזמנות באתר. אפשר להתקשר או לשלוח וואטסאפ.")
        cur.execute("SELECT id, name, price, stock_quantity, image_url FROM Products WHERE id = ANY(%s) AND category_id IS NOT NULL;",
                    (list(qty.keys()),))
        found = {r[0]: r for r in cur.fetchall()}
        missing = [pid for pid in qty if pid not in found]
        out = [found[pid][1] for pid in qty if pid in found and (found[pid][3] or 0) <= 0]
        if missing:
            raise HTTPException(status_code=409, detail="חלק מהמוצרים בעגלה כבר לא קיימים באתר. רעננו את הדף ונסו שוב.")
        if out:
            raise HTTPException(status_code=409, detail="אזלו מהמלאי: " + ", ".join(out[:5]) + ". צריך להוריד אותם מהעגלה.")

        user_id = sess["user_id"] if sess else None
        new_token = None
        if body.create_account and not sess:
            cur.execute("SELECT 1 FROM users WHERE email = %s;", (email,))
            if cur.fetchone():
                raise HTTPException(status_code=409, detail="המייל כבר רשום אצלנו. אפשר להתחבר, או להזמין בלי ליצור חשבון.")
            cur.execute("INSERT INTO users (name, email, phone, password_hash, role, notify_consent, notify_consent_at) "
                        "VALUES (%s,%s,%s,%s,'customer',%s, CASE WHEN %s THEN NOW() END) RETURNING id;",
                        (name, email, phone, hash_password(body.password), body.notify, body.notify))
            user_id = cur.fetchone()[0]
        elif sess:
            # Save the phone on the account if it was empty, so it's prefilled next time
            cur.execute("UPDATE users SET phone = COALESCE(NULLIF(phone, ''), %s) WHERE id = %s;", (phone, user_id))

        token = secrets.token_urlsafe(16)
        total = round(sum(float(found[pid][2]) * q for pid, q in qty.items()), 2)
        cur.execute("INSERT INTO orders (token, user_id, customer_name, phone, email, note, status, total) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'new',%s) RETURNING id;",
                    (token, user_id, name, phone, email, (body.note or "").strip()[:500] or None, total))
        order_id = cur.fetchone()[0]
        for pid, q in qty.items():
            _, pname, price, _, img = found[pid]
            cur.execute("INSERT INTO order_items (order_id, product_id, name, unit_price, quantity, image_url) VALUES (%s,%s,%s,%s,%s,%s);",
                        (order_id, pid, pname, price, q, img))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

    if body.create_account and not sess and user_id:
        new_token = _issue_token(user_id, "customer")
    number = _order_number(order_id)
    link = _track_link(order_id, token)
    count = sum(qty.values())

    def _notify():
        try:
            push_to_staff(f"🛒 הזמנה חדשה #{number}", f"{name} · {count} פריטים · ₪{total:.2f}", "/staff.html")
        except Exception as e:
            print(f"[ORDER] staff push failed: {e}")
        if email:
            _send_order_email(email, f"קיבלנו את ההזמנה שלך #{number}",
                              [f"שלום {name.split()[0] if name.split() else ''},",
                               f"קיבלנו את ההזמנה שלך (מספר {number}), {count} פריטים בסך ₪{total:.2f}.",
                               "נעדכן אותך כאן כשההזמנה מוכנה לאיסוף. התשלום בחנות בזמן האיסוף.",
                               f"כתובת לאיסוף: {_get_contact()['address']}"], link)
        else:
            _log_notify("email", f"לקוח: {name}", f"קיבלנו את ההזמנה שלך #{number}", False, "הלקוח לא השאיר מייל, ולכן לא נשלח אליו מייל אישור")
        store_mail = _get_contact().get("email")
        if store_mail:
            _send_order_email(store_mail, f"הזמנה חדשה באתר #{number}",
                              [f"התקבלה הזמנה חדשה מ-{name} ({phone}).", f"{count} פריטים, ₪{total:.2f}.",
                               "פרטי ההזמנה במסך הצוות באתר."], f"{SITE_URL}/staff.html")
    threading.Thread(target=_notify, daemon=True).start()

    resp = {"id": order_id, "number": number, "token": token, "total": total, "track_url": link}
    if new_token:
        conn = get_conn(); cur = conn.cursor(); u = _fetch_user(cur, user_id); cur.close(); conn.close()
        resp.update({"auth_token": new_token, "user": _user_public(u)})
        if body.notify:
            threading.Thread(target=_send_welcome_email, args=(email, name, user_id), daemon=True).start()
    return resp

@app.get("/api/orders/{order_id}")
def track_order(order_id: int, t: str):
    """Customer tracking page. Protected by a random token (t) that appears only in the customer's link."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE id = %s;", (order_id,))
    row = cur.fetchone()
    if not row or not hmac.compare_digest(row[1], t or ""):
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="ההזמנה לא נמצאה")
    d = _order_dict(cur, row); cur.close(); conn.close()
    d["address"] = _get_contact()["address"]
    return d

@app.get("/api/my/orders")
def my_orders(sess: dict = Depends(require_auth)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE user_id = %s ORDER BY id DESC LIMIT 30;", (sess["user_id"],))
    rows = cur.fetchall()
    out = []
    for r in rows:
        d = _order_dict(cur, r); d["token"] = r[1]; out.append(d)
    cur.close(); conn.close()
    return out

# ---------- Staff: order management ----------
@app.get("/api/staff/orders")
def staff_orders(scope: str = "active", sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    if scope == "done":
        cur.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE status IN ('picked_up', 'cancelled') "
                    "AND updated_at > NOW() - INTERVAL '7 days' ORDER BY updated_at DESC LIMIT 100;")
    else:
        cur.execute(f"SELECT {_ORDER_COLS} FROM orders WHERE status IN ('new', 'preparing', 'ready') ORDER BY id;")
    rows = cur.fetchall()
    items = [_order_dict(cur, r, staff=True) for r in rows]
    open_ = _orders_open(cur)
    cur.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE status = 'picked_up' "
                "AND picked_up_at >= date_trunc('day', NOW() AT TIME ZONE 'Asia/Jerusalem') AT TIME ZONE 'Asia/Jerusalem';")
    today_count, today_total = cur.fetchone()
    cur.close(); conn.close()
    return {"orders": items, "orders_open": open_, "today_picked_up": today_count, "today_total": float(today_total)}

class OrderStatusBody(BaseModel):
    status: str

@app.patch("/api/staff/orders/{order_id}")
def staff_update_order(order_id: int, body: OrderStatusBody, sess: dict = Depends(require_staff)):
    if body.status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="סטטוס לא תקין")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status, token, user_id, customer_name, email, total FROM orders WHERE id = %s FOR UPDATE;", (order_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="ההזמנה לא נמצאה")
    old, token, user_id, name, email, total = row
    cur.execute("UPDATE orders SET status = %s, updated_at = NOW(), "
                "ready_at = CASE WHEN %s = 'ready' THEN NOW() ELSE ready_at END, "
                "picked_up_at = CASE WHEN %s = 'picked_up' THEN NOW() ELSE picked_up_at END, "
                "handled_by = %s WHERE id = %s;", (body.status, body.status, body.status, sess["user_id"], order_id))
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "order_status", "order", order_id, {"from": old, "to": body.status})
    number, link = _order_number(order_id), _track_link(order_id, token)
    if body.status in ("ready", "cancelled") and body.status != old:
        if body.status == "ready":
            title, text = f"ההזמנה שלך מוכנה לאיסוף! 🎉", f"הזמנה #{number} מחכה לך ב-{_get_contact()['address']}. התשלום בחנות (₪{float(total):.2f})."
        else:
            title, text = f"הזמנה #{number} בוטלה", f"ההזמנה בוטלה. לשאלות אפשר להתקשר אלינו: {_get_contact()['phone']}."
        def _n():
            _push_order(order_id, user_id, title, text, f"/#order={order_id}&t={token}")
            if email:
                _send_order_email(email, title, [f"שלום {name.split()[0] if name.split() else ''},", text], link)
        threading.Thread(target=_n, daemon=True).start()
    return {"ok": True}

class OrderItemBody(BaseModel):
    missing: bool

@app.patch("/api/staff/orders/{order_id}/items/{item_id}")
def staff_update_order_item(order_id: int, item_id: int, body: OrderItemBody, sess: dict = Depends(require_staff)):
    """Marks an item missing in the store. The order total updates accordingly."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE order_items SET missing = %s, customer_choice = NULL, customer_note = NULL "
                "WHERE id = %s AND order_id = %s RETURNING name;", (body.missing, item_id, order_id))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="הפריט לא נמצא")
    if not body.missing:
        # The item turned up after all: drop the substitute the customer picked for it
        cur.execute("DELETE FROM order_items WHERE order_id = %s AND substitute_for = %s;", (order_id, item_id))
    _recalc_total(cur, order_id)
    cur.execute("SELECT total, status FROM orders WHERE id = %s;", (order_id,))
    total, status = cur.fetchone(); total = float(total)
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "order_item_missing", "order", order_id, {"item": r[0], "missing": body.missing})
    if body.missing and status in ORDER_ACTIVE:
        _schedule_missing_notice(order_id)
    return {"ok": True, "total": total}

# When an item is marked missing, wait a little before notifying the customer, so several missing items share one message
MISSING_NOTICE_DELAY = int(os.environ.get("MISSING_NOTICE_DELAY", "45"))
_missing_timers = {}
_missing_lock = threading.Lock()

def _schedule_missing_notice(order_id):
    with _missing_lock:
        if order_id in _missing_timers:
            return
        t = threading.Timer(MISSING_NOTICE_DELAY, _send_missing_notice, args=(order_id,))
        t.daemon = True
        _missing_timers[order_id] = t
        t.start()

def _send_missing_notice(order_id):
    with _missing_lock:
        _missing_timers.pop(order_id, None)
    try:
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT token, user_id, customer_name, email, status FROM orders WHERE id = %s;", (order_id,))
        o = cur.fetchone()
        cur.execute("SELECT name FROM order_items WHERE order_id = %s AND missing AND customer_choice IS NULL ORDER BY id;", (order_id,))
        names = [r[0] for r in cur.fetchall()]
        cur.close(); conn.close()
        if not o or o[4] not in ORDER_ACTIVE or not names:
            return
        token, user_id, name, email, _ = o
        number, link = _order_number(order_id), _track_link(order_id, token)
        listed = ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
        title = f"חסר מוצר בהזמנה #{number}"
        text = f"לצערנו חסר בחנות: {listed}. מה תעדיפו? להמשיך בלעדיו, לבקש תחליף, או לבטל את ההזמנה. בוחרים בקישור."
        _push_order(order_id, user_id, title, text, f"/#order={order_id}&t={token}")
        if email:
            _send_order_email(email, title, [f"שלום {name.split()[0] if name.split() else ''},", f"לצערנו חסר בחנות: {listed}.",
                                             "מה תעדיפו? להמשיך בלי המוצר, לבקש תחליף, או לבטל את ההזמנה.",
                                             "בוחרים בלחיצה על הכפתור למטה. אם לא נשמע מכם, נמשיך בלי המוצר החסר."], link)
    except Exception as e:
        print(f"[ORDER] missing notice failed: {e}")

class MissingResponseBody(BaseModel):
    choice: str                     # remove | replace | cancel
    item_id: Optional[int] = None
    note: Optional[str] = None

@app.post("/api/orders/{order_id}/respond")
def customer_missing_response(order_id: int, t: str, body: MissingResponseBody):
    """The customer decides about a missing item: continue without it, substitute (with a note), or cancel the whole order."""
    if body.choice not in ("remove", "replace", "cancel"):
        raise HTTPException(status_code=400, detail="בחירה לא תקינה")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT token, status FROM orders WHERE id = %s FOR UPDATE;", (order_id,))
    o = cur.fetchone()
    if not o or not hmac.compare_digest(o[0], t or ""):
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="ההזמנה לא נמצאה")
    if o[1] not in ("new", "preparing"):
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="ההזמנה כבר מוכנה או סגורה, ולכן אי אפשר לשנות אותה מכאן. אפשר להתקשר אלינו.")
    number = _order_number(order_id)
    if body.choice == "cancel":
        cur.execute("UPDATE orders SET status = 'cancelled', updated_at = NOW() WHERE id = %s;", (order_id,))
        msg = f"הלקוח ביטל את הזמנה #{number} בגלל פריט חסר"
    else:
        note = (body.note or "").strip()[:300] or None
        if body.choice == "replace" and not note:
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail="כתבו מה תרצו במקום, כדי שנדע מה להכין")
        cur.execute("UPDATE order_items SET customer_choice = %s, customer_note = %s WHERE id = %s AND order_id = %s AND missing RETURNING name;",
                    (body.choice, note if body.choice == "replace" else None, body.item_id, order_id))
        r = cur.fetchone()
        if not r:
            cur.close(); conn.close()
            raise HTTPException(status_code=404, detail="הפריט לא נמצא")
        # The customer changed their mind: drop the substitute they picked earlier for this item
        cur.execute("DELETE FROM order_items WHERE order_id = %s AND substitute_for = %s;", (order_id, body.item_id))
        _recalc_total(cur, order_id)
        msg = (f"#{number}: במקום \"{r[0]}\" הלקוח מבקש: {note}" if body.choice == "replace"
               else f"#{number}: הלקוח ממשיך בלי \"{r[0]}\"")
        cur.execute("UPDATE orders SET updated_at = NOW() WHERE id = %s;", (order_id,))
    conn.commit(); cur.close(); conn.close()
    _audit(None, "order_customer_response", "order", order_id, {"choice": body.choice, "item_id": body.item_id})
    threading.Thread(target=push_to_staff, args=("💬 תשובה מלקוח", msg[:180], "/staff.html"), daemon=True).start()
    return {"ok": True}

class SubstituteBody(BaseModel):
    item_id: int
    product_id: int
    quantity: int = 1

@app.post("/api/orders/{order_id}/substitute")
def customer_pick_substitute(order_id: int, t: str, body: SubstituteBody):
    """The customer picks a store product to replace a missing item. Price comes from the database and the total updates."""
    if not (1 <= body.quantity <= ORDER_MAX_QTY):
        raise HTTPException(status_code=400, detail="כמות לא תקינה")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT token, status FROM orders WHERE id = %s FOR UPDATE;", (order_id,))
    o = cur.fetchone()
    if not o or not hmac.compare_digest(o[0], t or ""):
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="ההזמנה לא נמצאה")
    if o[1] not in ("new", "preparing"):
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="ההזמנה כבר מוכנה או סגורה, ולכן אי אפשר לשנות אותה מכאן. אפשר להתקשר אלינו.")
    cur.execute("SELECT name FROM order_items WHERE id = %s AND order_id = %s AND missing;", (body.item_id, order_id))
    miss = cur.fetchone()
    if not miss:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="הפריט החסר לא נמצא")
    cur.execute("SELECT name, price, stock_quantity, image_url FROM Products WHERE id = %s AND category_id IS NOT NULL;", (body.product_id,))
    p = cur.fetchone()
    if not p or (p[2] or 0) <= 0:
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="המוצר הזה לא זמין כרגע. אפשר לבחור מוצר אחר.")
    cur.execute("DELETE FROM order_items WHERE order_id = %s AND substitute_for = %s;", (order_id, body.item_id))
    cur.execute("INSERT INTO order_items (order_id, product_id, name, unit_price, quantity, image_url, substitute_for) VALUES (%s,%s,%s,%s,%s,%s,%s);",
                (order_id, body.product_id, p[0], p[1], body.quantity, p[3], body.item_id))
    note = f"{p[0]} × {body.quantity}"
    cur.execute("UPDATE order_items SET customer_choice = 'replace', customer_note = %s WHERE id = %s;", (note, body.item_id))
    _recalc_total(cur, order_id)
    conn.commit(); cur.close(); conn.close()
    _audit(None, "order_customer_substitute", "order", order_id, {"missing": miss[0], "picked": p[0], "qty": body.quantity})
    threading.Thread(target=push_to_staff, args=("🔄 הלקוח בחר תחליף", f"#{_order_number(order_id)}: במקום \"{miss[0]}\" ← {note}"[:180], "/staff.html"), daemon=True).start()
    return {"ok": True}

class AddItemBody(BaseModel):
    name: str
    unit_price: float
    quantity: int = 1
    product_id: Optional[int] = None

@app.post("/api/staff/orders/{order_id}/items")
def staff_add_order_item(order_id: int, body: AddItemBody, sess: dict = Depends(require_staff)):
    """Adds an item to an order (usually a substitute for a missing one). The total updates."""
    name = (body.name or "").strip()[:300]
    if not name or body.unit_price < 0 or body.unit_price > 10000 or not (1 <= body.quantity <= ORDER_MAX_QTY):
        raise HTTPException(status_code=400, detail="צריך שם, מחיר וכמות תקינים")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE;", (order_id,))
    o = cur.fetchone()
    if not o or o[0] not in ORDER_ACTIVE:
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="אפשר להוסיף פריטים רק להזמנה פעילה")
    img = None
    if body.product_id:
        cur.execute("SELECT image_url FROM Products WHERE id = %s;", (body.product_id,))
        r = cur.fetchone(); img = r[0] if r else None
    cur.execute("INSERT INTO order_items (order_id, product_id, name, unit_price, quantity, image_url, added_by_staff) "
                "VALUES (%s,%s,%s,%s,%s,%s,TRUE) RETURNING id;", (order_id, body.product_id, name, round(body.unit_price, 2), body.quantity, img))
    new_id = cur.fetchone()[0]
    _recalc_total(cur, order_id)
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "order_item_add", "order", order_id, {"name": name, "price": body.unit_price, "qty": body.quantity})
    return {"ok": True, "id": new_id}

@app.delete("/api/staff/orders/{order_id}/items/{item_id}")
def staff_remove_added_item(order_id: int, item_id: int, sess: dict = Depends(require_staff)):
    """Removes an item staff added (a wrong substitute). Items the customer ordered are never deleted, only marked missing."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM order_items WHERE id = %s AND order_id = %s AND added_by_staff RETURNING name;", (item_id, order_id))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="אפשר להסיר רק פריט שהצוות הוסיף")
    _recalc_total(cur, order_id)
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "order_item_remove", "order", order_id, {"name": r[0]})
    return {"ok": True}

@app.get("/api/staff/product-search")
def staff_product_search(q: str = "", sess: dict = Depends(require_staff)):
    """Product search for choosing a substitute."""
    q = q.strip()
    if len(q) < 2:
        return []
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, name, price, stock_quantity > 0 FROM Products WHERE name ILIKE %s AND category_id IS NOT NULL "
                "ORDER BY (stock_quantity > 0) DESC, name LIMIT 12;", (f"%{q}%",))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r[0], "name": r[1], "price": float(r[2]), "in_stock": bool(r[3])} for r in rows]

class StockBody(BaseModel):
    in_stock: bool

@app.post("/api/staff/products/{product_id}/stock")
def staff_set_stock(product_id: int, body: StockBody, sess: dict = Depends(require_staff)):
    """Staff can mark a product out of stock (or back) when they notice it while picking an order."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE Products SET stock_quantity = %s WHERE id = %s RETURNING name;", (100 if body.in_stock else 0, product_id))
    r = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="המוצר לא נמצא")
    _audit(sess, "stock_change", "product", product_id, {"name": r[0], "in_stock": body.in_stock})
    return {"ok": True}

class OrdersOpenBody(BaseModel):
    open: bool

@app.put("/api/staff/orders-open")
def staff_set_orders_open(body: OrdersOpenBody, sess: dict = Depends(require_staff)):
    conn = get_conn(); cur = conn.cursor()
    _setting_set(cur, "orders_open", "on" if body.open else "off")
    conn.commit(); cur.close(); conn.close()
    _audit(sess, "orders_open", "settings", None, {"open": body.open})
    return {"ok": True}


# ===================== Reviews =====================
# Signed-in customers can write a review (0-5 stars). It appears on the site only after an admin approves it.
# Signed-in users can reply to approved reviews. Replies appear immediately, and admins can delete them.
REVIEW_ATTEMPTS = defaultdict(list)
REPLY_ATTEMPTS = defaultdict(list)

def _simple_rate_limit(bucket, ip, max_n, window, msg):
    now = time_module.time()
    bucket[ip] = [t for t in bucket[ip] if now - t < window]
    if len(bucket[ip]) >= max_n:
        raise HTTPException(status_code=429, detail=msg)
    bucket[ip].append(now)

def _clean_text(t, max_len):
    """Trims extra whitespace but keeps line breaks (at most two in a row)."""
    lines = [" ".join(l.split()) for l in (t or "").splitlines()]
    out = "\n".join(lines).strip()
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out[:max_len].strip()

def _looks_like_spam(t):
    import re
    return bool(re.search(r"https?://|www\.|\.com\b|\.co\.il\b|t\.me/", t or "", re.I))

def _short_name(name):
    """First name + last-name initial ("שירה כ."), so it looks real while protecting privacy."""
    parts = (name or "").split()
    if not parts:
        return "לקוח/ה"
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]

def _replies_for(cur, review_ids):
    if not review_ids:
        return {}
    cur.execute("""SELECT rr.id, rr.review_id, rr.name, rr.text, rr.is_store, rr.created_at,
                          EXISTS (SELECT 1 FROM orders o WHERE o.user_id = rr.user_id AND o.status = 'picked_up'),
                          u.avatar_url
                   FROM review_replies rr LEFT JOIN users u ON u.id = rr.user_id
                   WHERE rr.review_id = ANY(%s) ORDER BY rr.id;""", (review_ids,))
    out = defaultdict(list)
    for r in cur.fetchall():
        out[r[1]].append({"id": r[0], "name": "Bake & Party" if r[4] else _short_name(r[2]), "text": r[3], "is_store": bool(r[4]),
                          "verified": bool(r[6]), "avatar": None if r[4] else r[7],
                          "created_at": _il(r[5]).strftime("%d/%m/%Y") if r[5] else ""})
    return out

_REVIEW_SELECT = """SELECT r.id, r.name, r.rating, r.text, r.status, r.created_at, r.photo_url, r.user_id, u.created_at, u.email,
           EXISTS (SELECT 1 FROM orders o WHERE o.user_id = r.user_id AND o.status = 'picked_up'),
           (SELECT COUNT(*) FROM reviews r2 WHERE r.user_id IS NOT NULL AND r2.user_id = r.user_id AND r2.status = 'approved'),
           u.avatar_url, u.google_sub IS NOT NULL
    FROM reviews r LEFT JOIN users u ON u.id = r.user_id"""

def _review_dict(r, replies, admin=False):
    d = {"id": r[0], "name": _short_name(r[1]), "rating": r[2], "text": r[3] or "", "status": r[4],
         "created_at": _il(r[5]).strftime("%d/%m/%Y") if r[5] else "", "photo": r[6],
         "has_account": bool(r[7]), "verified": bool(r[10]), "user_reviews": int(r[11] or 0),
         "member_since": r[8].strftime("%m/%Y") if r[8] else None, "replies": replies.get(r[0], []),
         "avatar": r[12], "google": bool(r[13])}
    if admin:
        d.update({"full_name": r[1], "email": r[9] or ""})
    return d

@app.get("/api/reviews")
def list_reviews(limit: int = 10, offset: int = 0):
    limit = max(1, min(limit, 30)); offset = max(0, offset)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*), COALESCE(AVG(rating), 0) FROM reviews WHERE status = 'approved';")
    count, avg = cur.fetchone()
    cur.execute("SELECT rating, COUNT(*) FROM reviews WHERE status = 'approved' GROUP BY rating;")
    dist = {int(r[0]): r[1] for r in cur.fetchall()}
    cur.execute(_REVIEW_SELECT + " WHERE r.status = 'approved' ORDER BY r.id DESC LIMIT %s OFFSET %s;", (limit, offset))
    rows = cur.fetchall()
    replies = _replies_for(cur, [r[0] for r in rows])
    cur.close(); conn.close()
    return {"count": count, "average": round(float(avg), 1), "distribution": {str(k): dist.get(k, 0) for k in range(6)},
            "items": [_review_dict(r, replies) for r in rows]}

REVIEW_PHOTO_MAX = 6 * 1024 * 1024
REVIEW_PHOTO_ATTEMPTS = defaultdict(list)

def _image_kind(data):
    """Checks by the file's content (not just its name) that it really is an image."""
    if data[:3] == b"\xff\xd8\xff": return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"): return "gif"
    return None

@app.post("/api/reviews/photo")
async def upload_review_photo(file: UploadFile = File(...), sess: dict = Depends(require_auth)):
    """Optional photo for a review. Signed-in users only; shown only once the review is approved."""
    _simple_rate_limit(REVIEW_PHOTO_ATTEMPTS, str(sess["user_id"]), 10, 3600, "הועלו הרבה תמונות. אפשר לנסות שוב מאוחר יותר.")
    data = await file.read(REVIEW_PHOTO_MAX + 1)
    if len(data) > REVIEW_PHOTO_MAX:
        raise HTTPException(status_code=400, detail="התמונה גדולה מדי (עד 6MB)")
    kind = _image_kind(data)
    if not kind:
        raise HTTPException(status_code=400, detail="אפשר להעלות רק תמונה (jpg, png, webp)")
    name = f"rv_{uuid.uuid4().hex}.{kind}"
    with open(os.path.join(IMAGES_DIR, name), "wb") as f:
        f.write(data)
    return {"url": f"/images/{name}"}

class ReviewBody(BaseModel):
    rating: int
    text: Optional[str] = None
    photo_url: Optional[str] = None
    name: Optional[str] = None         # Not used: the name comes from the account
    website: Optional[str] = None      # Honeypot for bots: a hidden field that people never fill in

@app.post("/api/reviews")
def create_review(body: ReviewBody, request: Request, authorization: str = Header(None)):
    """Reviews only from a registered account: the name comes from the profile, so reviews are genuine."""
    if body.website:
        return {"ok": True, "pending": True}
    sess = _session_from_header(authorization)
    if not sess:
        raise HTTPException(status_code=401, detail="כדי לכתוב ביקורת צריך להתחבר (או להירשם בחינם)")
    text = _clean_text(body.text, 1500)
    if body.rating < 0 or body.rating > 5:
        raise HTTPException(status_code=400, detail="הדירוג צריך להיות בין 0 ל-5 כוכבים")
    if _looks_like_spam(text):
        raise HTTPException(status_code=400, detail="אי אפשר לכלול קישורים בביקורת")
    photo = (body.photo_url or "").strip() or None
    if photo and not (photo.startswith("/images/rv_") and "/" not in photo[len("/images/"):]):
        raise HTTPException(status_code=400, detail="תמונה לא תקינה")
    ip = request.client.host if request.client else "unknown"
    _simple_rate_limit(REVIEW_ATTEMPTS, ip, 3, 3600, "כבר נשלחו כמה ביקורות מהמכשיר הזה. אפשר לנסות שוב מאוחר יותר.")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM reviews WHERE user_id = %s AND status <> 'rejected' AND created_at > NOW() - INTERVAL '30 days';", (sess["user_id"],))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=409, detail="כבר כתבת לנו ביקורת לאחרונה. תודה רבה! 💗")
    cur.execute("SELECT name, email FROM users WHERE id = %s;", (sess["user_id"],))
    uname, uemail = cur.fetchone()
    name = (uname or "").strip() or (uemail or "").split("@")[0] or "לקוח/ה"
    cur.execute("INSERT INTO reviews (user_id, name, rating, text, photo_url) VALUES (%s,%s,%s,%s,%s) RETURNING id;",
                (sess["user_id"], name[:80], body.rating, text or None, photo))
    rid = cur.fetchone()[0]
    conn.commit(); cur.close(); conn.close()
    threading.Thread(target=push_to_admins, args=("⭐ ביקורת חדשה ממתינה לאישור", f"{_short_name(name)}: {body.rating} כוכבים", "/admin.html"), daemon=True).start()
    return {"ok": True, "pending": True, "id": rid}

class ReplyBody(BaseModel):
    text: str
    name: Optional[str] = None         # Not used: the name comes from the account
    website: Optional[str] = None

@app.post("/api/reviews/{review_id}/replies")
def create_reply(review_id: int, body: ReplyBody, request: Request, authorization: str = Header(None)):
    if body.website:
        return {"ok": True}
    sess = _session_from_header(authorization)
    if not sess:
        raise HTTPException(status_code=401, detail="כדי להגיב צריך להתחבר (או להירשם בחינם)")
    text = _clean_text(body.text, 800)
    if not text:
        raise HTTPException(status_code=400, detail="צריך לכתוב תגובה")
    if _looks_like_spam(text):
        raise HTTPException(status_code=400, detail="אי אפשר לכלול קישורים בתגובה")
    ip = request.client.host if request.client else "unknown"
    _simple_rate_limit(REPLY_ATTEMPTS, ip, 6, 3600, "נשלחו הרבה תגובות מהמכשיר הזה. אפשר לנסות שוב מאוחר יותר.")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM reviews WHERE id = %s AND status = 'approved';", (review_id,))
    if not cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="הביקורת לא נמצאה")
    is_store = sess["role"] in STAFF_ROLES
    cur.execute("SELECT name, email FROM users WHERE id = %s;", (sess["user_id"],))
    uname, uemail = cur.fetchone()
    name = "Bake & Party" if is_store else ((uname or "").strip() or (uemail or "").split("@")[0] or "לקוח/ה")
    cur.execute("INSERT INTO review_replies (review_id, user_id, name, text, is_store) VALUES (%s,%s,%s,%s,%s) RETURNING id;",
                (review_id, sess["user_id"], name[:80], text, is_store))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.get("/api/admin/reviews")
def admin_reviews(status: str = "pending", sess: dict = Depends(require_admin)):
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="סטטוס לא תקין")
    conn = get_conn(); cur = conn.cursor()
    cur.execute(_REVIEW_SELECT + " WHERE r.status = %s ORDER BY r.id DESC LIMIT 200;", (status,))
    rows = cur.fetchall()
    replies = _replies_for(cur, [r[0] for r in rows])
    cur.execute("SELECT status, COUNT(*) FROM reviews GROUP BY status;")
    counts = dict(cur.fetchall())
    cur.close(); conn.close()
    return {"counts": {k: counts.get(k, 0) for k in ("pending", "approved", "rejected")}, "items": [_review_dict(r, replies, admin=True) for r in rows]}

class ReviewStatusBody(BaseModel):
    status: str

@app.patch("/api/admin/reviews/{review_id}")
def admin_review_status(review_id: int, body: ReviewStatusBody, sess: dict = Depends(require_admin)):
    if body.status not in ("pending", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="סטטוס לא תקין")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE reviews SET status = %s, reviewed_at = NOW(), reviewed_by = %s WHERE id = %s RETURNING id;",
                (body.status, sess["user_id"], review_id))
    ok = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="הביקורת לא נמצאה")
    _audit(sess, "review_status", "review", review_id, {"status": body.status})
    return {"ok": True}

@app.delete("/api/admin/reviews/{review_id}")
def admin_delete_review(review_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM reviews WHERE id = %s RETURNING name;", (review_id,))
    r = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="הביקורת לא נמצאה")
    _audit(sess, "review_delete", "review", review_id, {"name": r[0]})
    return {"ok": True}

@app.delete("/api/admin/review-replies/{reply_id}")
def admin_delete_reply(reply_id: int, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM review_replies WHERE id = %s RETURNING review_id;", (reply_id,))
    r = cur.fetchone(); conn.commit(); cur.close(); conn.close()
    if not r:
        raise HTTPException(status_code=404, detail="התגובה לא נמצאה")
    _audit(sess, "review_reply_delete", "review", r[0], {"reply_id": reply_id})
    return {"ok": True}


# --- PWA files: installable app + notifications ---
@app.get("/sw.js")
def serve_sw():
    return FileResponse(os.path.join(STATIC_DIR, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

@app.get("/manifest.webmanifest")
def serve_manifest():
    return FileResponse(os.path.join(STATIC_DIR, "manifest.webmanifest"), media_type="application/manifest+json")

@app.get("/manifest-staff.webmanifest")
def serve_manifest_staff():
    return FileResponse(os.path.join(STATIC_DIR, "manifest-staff.webmanifest"), media_type="application/manifest+json")

@app.get("/icon-192.png")
def serve_icon192():
    return FileResponse(os.path.join(STATIC_DIR, "icon-192.png"), media_type="image/png")

@app.get("/icon-512.png")
def serve_icon512():
    return FileResponse(os.path.join(STATIC_DIR, "icon-512.png"), media_type="image/png")

# --- Import products from a Wolt Excel export (preview, then apply) ---
# Never deletes anything: only adds new products and categories. An existing product (matched by name) is left as is.
_WOLT_PSEUDO_CATS = {"חדש בחנות", "חדש_בחנות", "מוצרים מובילים", "מוצרים_מובילים",
                     "חדש בחנות ומוצרים מובילים", "חדש_בחנות_ומוצרים_מובילים"}

def _wolt_price(raw, divide: bool, rounding: str):
    import math
    v = float(raw)
    if divide:
        v = v / 1.3
    v = round(v, 2)
    if rounding == "nine":      # Round up to the next x.9 (10.52 -> 10.9)
        v = max(0, math.ceil(round(v - 0.9, 2))) + 0.9
    elif rounding == "tenth":   # Round up to the next 10 agorot
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
    as_new: bool = False,
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
        # ---------- Categories ----------
        # Newer Wolt exports have no id column in the categories sheet, so categories are matched by name:
        # each id's name comes from the sheet (when it has ids) or from the products' category_id column ("name::id").
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

        # ---------- Products: merge duplicates (same name and price) ----------
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

        cur.execute("SELECT COUNT(*) FROM Products;")
        site_was_empty = cur.fetchone()[0] == 0
        if site_was_empty:
            as_new = False        # First import into an empty site: not "new in store" and no announcement
        added = skipped = updated = uncategorized = multi = 0
        samples = []
        for (nk, _), m in merged.items():
            price = _wolt_price(m["wolt_price"], divide, rounding)
            if nk in existing:   # existing = what the site had before this import
                skipped += 1
                if update_prices:
                    cur.execute("UPDATE Products SET price = %s WHERE id = %s;", (price, existing[nk])); updated += 1
                continue
            primary = m["cats"][0] if m["cats"] else None
            cur.execute(
                "INSERT INTO Products (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s, (NOW() - %s::interval)::timestamp) RETURNING id;",
                (m["name"], primary, price, m["stock"], m["weight"], m["units"], m["image"], m["notes"],
                 "0 days" if as_new else "400 days"))
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
              "uncategorized": uncategorized, "multi_category": multi, "samples": samples, "announced_as_new": bool(as_new)}
    if mode == "apply":
        _audit(sess, "wolt_import", "products", None, {k: v for k, v in result.items() if k != "samples"})
    return result


# --- Quick assignment of a preset deal ---
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





# --- SEO: crawlable product and category pages, sitemap and store information ---
# All database text goes through html.escape, and JSON-LD is built with json.dumps, so a name with quotes
# (e.g. 22*17 cm written with a Hebrew gershayim) can't break the page or the structured data.
from fastapi.responses import Response
import html as _html

SITE_NAME = "Bake & Party"
SITE_FULL_NAME = "Bake & Party - אפייה ומסיבות | ממלכת הגלידות מבית פינוקים"
WOLT_URL = "https://wolt.com/he/isr/petah-tikva/venue/pinookim-givat-shmuel"
GOOGLE_SITE_VERIFICATION = os.environ.get("GOOGLE_SITE_VERIFICATION", "").strip()
_DAY_SCHEMA = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

def _e(t):
    return _html.escape(str(t if t is not None else ""), quote=True)

def _abs_url(path_or_url):
    if not path_or_url:
        return f"{SITE_URL}/logo-web.jpg"
    if path_or_url.startswith("http"):
        return path_or_url
    return SITE_URL + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)

def _ld(obj):
    # "</" inside JSON-LD would close the script tag, so escape it
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

def _store_schema():
    """Physical store information in a format Google understands (LocalBusiness): address, phone and hours."""
    c = _get_contact()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT day_of_week, is_closed, opening_time, closing_time FROM weekly_hours ORDER BY day_of_week;")
    hours = [{"@type": "OpeningHoursSpecification", "dayOfWeek": _DAY_SCHEMA[d], "opens": op, "closes": cl}
             for d, closed, op, cl in cur.fetchall() if not closed]
    cur.close(); conn.close()
    street, _, city = c["address"].partition(",")
    phone = c["phone"]
    intl = "+972-" + phone.lstrip("0") if phone.startswith("0") else phone
    return {
        "@context": "https://schema.org",
        "@type": "Store",
        "@id": f"{SITE_URL}/#store",
        "name": SITE_NAME,
        "alternateName": ["ממלכת הגלידות מבית פינוקים", "פינוקים גבעת שמואל", "Bake and Party"],
        "description": "חנות אפייה ומסיבות בגבעת שמואל: חומרי גלם לאפייה, אביזרי אפייה, קישוטים לעוגות, בלונים ומוצרים למסיבות.",
        "url": SITE_URL + "/",
        "logo": f"{SITE_URL}/logo-web.jpg",
        "image": f"{SITE_URL}/logo-web.jpg",
        "telephone": intl,
        "email": c["email"],
        "address": {"@type": "PostalAddress", "streetAddress": street.strip(),
                    "addressLocality": (city.strip() or "גבעת שמואל"), "addressCountry": "IL"},
        "areaServed": ["גבעת שמואל", "פתח תקווה", "בני ברק", "רמת גן", "קריית אונו"],
        "openingHoursSpecification": hours,
        "sameAs": [WOLT_URL],
        "priceRange": "₪",
    }

def _seo_page(title, description, canonical, body, ld_list=(), og_image=None):
    verify = f'<meta name="google-site-verification" content="{_e(GOOGLE_SITE_VERIFICATION)}">' if GOOGLE_SITE_VERIFICATION else ""
    lds = "".join(f'<script type="application/ld+json">{_ld(x)}</script>' for x in ld_list)
    return f"""<!DOCTYPE html>
<html lang="he" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(canonical)}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(description)}">
<meta property="og:image" content="{_e(og_image or SITE_URL + '/logo-web.jpg')}">
<meta property="og:url" content="{_e(canonical)}">
<meta property="og:locale" content="he_IL">
<meta name="theme-color" content="#E05276">
<link rel="icon" href="/icon-192.png">
{verify}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;700&display=swap" rel="stylesheet">
{lds}
<style>
:root {{ --ink:#2B1C21; --muted:#7A6369; --line:#F1E3E7; --bg:#FFFAF7; --brand:#E05276; --brand-soft:#FFE4EC; --ok:#2E8B57; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:'Rubik',system-ui,sans-serif; background:var(--bg); color:var(--ink); }}
a {{ color:inherit; }}
.top {{ background:#fff; border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }}
.top-in {{ max-width:1100px; margin:auto; padding:10px 16px; display:flex; align-items:center; gap:12px; justify-content:space-between; }}
.brand {{ display:flex; align-items:center; gap:10px; text-decoration:none; font-weight:700; color:var(--brand); font-size:1.15em; }}
.brand img {{ width:42px; height:42px; border-radius:12px; object-fit:cover; }}
.pill {{ display:inline-block; background:var(--brand); color:#fff; text-decoration:none; padding:10px 18px; border-radius:999px; font-weight:700; }}
.pill.ghost {{ background:#fff; color:var(--brand); border:2px solid var(--brand-soft); }}
.wrap {{ max-width:1100px; margin:auto; padding:18px 16px 40px; }}
.crumbs {{ font-size:.9em; color:var(--muted); margin-bottom:14px; }}
.crumbs a {{ text-decoration:none; color:var(--brand); }}
.prod {{ display:grid; grid-template-columns:1fr 1fr; gap:28px; background:#fff; border-radius:24px; padding:24px; border:1px solid var(--line); }}
.prod img {{ width:100%; max-height:380px; object-fit:contain; background:#fff; border-radius:16px; }}
h1 {{ margin:0 0 8px; font-size:1.6em; line-height:1.3; }}
.price {{ font-size:1.8em; font-weight:700; color:var(--brand); margin:10px 0; }}
.stock {{ display:inline-block; padding:4px 12px; border-radius:999px; font-size:.9em; font-weight:500; background:#E7F5EC; color:var(--ok); }}
.stock.out {{ background:#F1F1F1; color:#888; }}
.desc {{ line-height:1.7; color:#4a3a3f; }}
.actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }}
h2 {{ font-size:1.25em; margin:30px 0 12px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:12px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:18px; padding:10px; text-decoration:none; display:flex; flex-direction:column; gap:6px; }}
.card img {{ width:100%; aspect-ratio:1; object-fit:contain; border-radius:12px; background:#fff; }}
.card b {{ font-weight:500; font-size:.92em; line-height:1.35; }}
.card span {{ color:var(--brand); font-weight:700; }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
.chips a {{ background:#fff; border:1px solid var(--line); border-radius:999px; padding:8px 14px; text-decoration:none; }}
.foot {{ text-align:center; color:var(--muted); font-size:.88em; padding:24px 16px; border-top:1px solid var(--line); }}
@media (max-width:700px) {{ .prod {{ grid-template-columns:1fr; padding:16px; }} h1 {{ font-size:1.3em; }} .grid {{ grid-template-columns:repeat(2,1fr); }} }}
</style>
</head>
<body>
<header class="top"><div class="top-in">
<a class="brand" href="/"><img src="/icon-192.png" alt="Bake & Party">Bake &amp; Party</a>
<a class="pill" href="/">לכל המוצרים</a>
</div></header>
<main class="wrap">{body}</main>
<footer class="foot">{_e(SITE_NAME)} · אפייה ומסיבות · {_e(_get_contact()["address"])} · {_e(_get_contact()["phone"])}</footer>
</body>
</html>"""

def _card_html(pid, name, price, image):
    return (f'<a class="card" href="/product/{pid}"><img loading="lazy" src="{_e(_abs_url(image) if image else "/icon-192.png")}" alt="{_e(name)}">'
            f'<b>{_e(name)}</b><span>₪{float(price):.2f}</span></a>')

def _category_chain(cur, cat_id):
    chain = []
    while cat_id:
        cur.execute("SELECT id, name, parent_id FROM Categories WHERE id = %s;", (cat_id,))
        r = cur.fetchone()
        if not r:
            break
        chain.insert(0, (r[0], r[1])); cat_id = r[2]
    return chain

def _breadcrumb_ld(items):
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": n, "item": u} for i, (n, u) in enumerate(items)]}

@app.get("/sitemap.xml")
def sitemap():
    """Sitemap: home page, every category and every product, so Google knows which pages exist."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM Categories ORDER BY id;")
    cats = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT id FROM Products WHERE category_id IS NOT NULL ORDER BY id;")
    prods = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    urls = [f"  <url><loc>{SITE_URL}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>"]
    urls.append(f"  <url><loc>{SITE_URL}/privacy</loc><changefreq>yearly</changefreq><priority>0.2</priority></url>")
    urls += [f"  <url><loc>{SITE_URL}/category/{c}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>" for c in cats]
    urls += [f"  <url><loc>{SITE_URL}/product/{p}</loc><changefreq>weekly</changefreq><priority>0.6</priority></url>" for p in prods]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + "\n".join(urls) + "\n</urlset>"
    return Response(content=xml, media_type="application/xml")

@app.get("/product/{product_id}")
def product_seo_page(product_id: int):
    """Crawlable product page: title, description, price, stock and structured data (schema.org)."""
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT p.id, p.name, p.price, p.stock_quantity, p.category_id, p.image_url, p.notes, "
                "p.weight_grams, p.units_per_package FROM Products p WHERE p.id = %s;", (product_id,))
    p = cur.fetchone()
    if not p:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="מוצר לא נמצא")
    pid, name, price, stock, cat_id, image, notes, weight, units = p
    price = float(price); in_stock = (stock or 0) > 0
    chain = _category_chain(cur, cat_id)
    related = []
    if cat_id:
        cur.execute("SELECT id, name, price, image_url FROM Products WHERE category_id = %s AND id <> %s "
                    "ORDER BY (stock_quantity > 0) DESC, id LIMIT 12;", (cat_id, pid))
        related = cur.fetchall()
    cur.close(); conn.close()

    cat_name = chain[-1][1] if chain else ""
    canonical = f"{SITE_URL}/product/{pid}"
    img_abs = _abs_url(image)
    desc_text = (notes or "").strip()
    meta_desc = (desc_text[:140] + " | " if desc_text else "") + f"{name} ב-₪{price:.2f}" + \
                (f" | {cat_name}" if cat_name else "") + " | Bake & Party, חנות אפייה ומסיבות בגבעת שמואל"
    # Google wants a real description. Most products have no written one, so build a useful sentence
    # from what we know (category, weight, pack size, pickup) instead of repeating the name.
    facts = [f"משקל {weight} גרם" if weight else "", f"{units} יחידות באריזה" if units else ""]
    auto_desc = (f"{name}" + (f" ({', '.join(f for f in facts if f)})" if any(facts) else "") +
                 (f", מקטגוריית {cat_name}" if cat_name else "") +
                 f". נמכר ב-{SITE_NAME}, חנות אפייה ומסיבות בגבעת שמואל. הזמנה באתר ואיסוף עצמי מהחנות.")
    product_ld = {
        "@context": "https://schema.org", "@type": "Product", "name": name, "image": [img_abs],
        "description": desc_text if len(desc_text) >= 40 else (f"{auto_desc} {desc_text}." if desc_text else auto_desc),
        "sku": str(pid),
        "category": " > ".join(n for _, n in chain) or None,
        "offers": {"@type": "Offer", "url": canonical, "price": f"{price:.2f}", "priceCurrency": "ILS",
                   "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock",
                   "itemCondition": "https://schema.org/NewCondition",
                   "seller": {"@id": f"{SITE_URL}/#store", "@type": "Store", "name": SITE_NAME}},
    }
    crumbs = [("דף הבית", SITE_URL + "/")] + [(n, f"{SITE_URL}/category/{cid}") for cid, n in chain] + [(name, canonical)]
    crumb_html = " › ".join([f'<a href="/">דף הבית</a>'] + [f'<a href="/category/{cid}">{_e(n)}</a>' for cid, n in chain] + [_e(name)])
    meta = []
    if weight: meta.append(f"משקל: {weight} גרם")
    if units: meta.append(f"יחידות באריזה: {units}")
    wa = _get_contact()["whatsapp"]
    wa_text = f"שלום, אשמח להזמין: {name}"
    body = f"""
<nav class="crumbs">{crumb_html}</nav>
<article class="prod">
  <img src="{_e(img_abs)}" alt="{_e(name)}">
  <div>
    <h1>{_e(name)}</h1>
    {f'<div style="color:var(--muted)">{_e(cat_name)}</div>' if cat_name else ''}
    <div class="price">₪{price:.2f}</div>
    <span class="stock{'' if in_stock else ' out'}">{'במלאי בחנות' if in_stock else 'אזל מהמלאי'}</span>
    {f'<p class="desc">{_e(desc_text)}</p>' if desc_text else ''}
    {f'<p class="desc">{_e(" · ".join(meta))}</p>' if meta else ''}
    <div class="actions">
      <a class="pill" href="/#p={pid}">לצפייה בחנות האונליין</a>
      <a class="pill ghost" href="https://wa.me/{_e(wa)}?text={_e(_url_quote(wa_text))}" rel="noopener">הזמנה בוואטסאפ</a>
    </div>
  </div>
</article>
{('<h2>עוד ב' + _e(cat_name) + '</h2><div class="grid">' + "".join(_card_html(*r) for r in related) + '</div>') if related else ''}
"""
    html = _seo_page(f"{name} | {SITE_NAME} גבעת שמואל", meta_desc, canonical, body,
                     [product_ld, _breadcrumb_ld(crumbs)], og_image=img_abs)
    return Response(content=html, media_type="text/html")

def _url_quote(t):
    from urllib.parse import quote
    return quote(t)

@app.get("/category/{category_id}")
def category_seo_page(category_id: int):
    """Crawlable category page: every product in the category, linking to each product page."""
    conn = get_conn(); cur = conn.cursor()
    chain = _category_chain(cur, category_id)
    if not chain:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="קטגוריה לא נמצאה")
    cur.execute("SELECT id, name FROM Categories WHERE parent_id = %s ORDER BY sort_order, id;", (category_id,))
    subs = cur.fetchall()
    cur.execute("""
        WITH RECURSIVE subtree AS (
            SELECT id FROM Categories WHERE id = %s
            UNION ALL SELECT c.id FROM Categories c JOIN subtree s ON c.parent_id = s.id)
        SELECT DISTINCT p.id, p.name, p.price, p.image_url, (p.stock_quantity > 0) AS ok
        FROM Products p
        WHERE p.category_id IN (SELECT id FROM subtree)
           OR EXISTS (SELECT 1 FROM product_categories pc WHERE pc.product_id = p.id AND pc.category_id IN (SELECT id FROM subtree))
        ORDER BY ok DESC, p.id LIMIT 300;
    """, (category_id,))
    prods = cur.fetchall()
    cur.close(); conn.close()
    name = chain[-1][1]
    canonical = f"{SITE_URL}/category/{category_id}"
    crumbs = [("דף הבית", SITE_URL + "/")] + [(n, f"{SITE_URL}/category/{cid}") for cid, n in chain]
    crumb_html = " › ".join([f'<a href="/">דף הבית</a>'] + [f'<a href="/category/{cid}">{_e(n)}</a>' for cid, n in chain[:-1]] + [_e(name)])
    sample = ", ".join(r[1] for r in prods[:4])
    meta_desc = f"{name} בגבעת שמואל: {len(prods)} מוצרים" + (f", כמו {sample}" if sample else "") + ". Bake & Party, חנות אפייה ומסיבות."
    list_ld = {"@context": "https://schema.org", "@type": "ItemList", "name": name,
               "itemListElement": [{"@type": "ListItem", "position": i + 1, "url": f"{SITE_URL}/product/{r[0]}", "name": r[1]}
                                   for i, r in enumerate(prods[:100])]}
    body = f"""
<nav class="crumbs">{crumb_html}</nav>
<h1>{_e(name)} <span style="font-weight:400;color:var(--muted);font-size:.65em">ב-Bake &amp; Party גבעת שמואל</span></h1>
{('<div class="chips" style="margin:14px 0">' + "".join(f'<a href="/category/{sid}">{_e(sn)}</a>' for sid, sn in subs) + '</div>') if subs else ''}
<div class="actions" style="margin:10px 0 20px"><a class="pill" href="/#c={category_id}">לקנייה באתר</a></div>
<div class="grid">{"".join(_card_html(r[0], r[1], r[2], r[3]) for r in prods) or '<p>אין כרגע מוצרים בקטגוריה הזו.</p>'}</div>
"""
    html = _seo_page(f"{name} | {SITE_NAME} גבעת שמואל", meta_desc, canonical, body, [list_ld, _breadcrumb_ld(crumbs)])
    return Response(content=html, media_type="text/html")

def _render_index_html():
    """index.html with store information for Google (Store) and the Search Console verification tag if configured."""
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    extra = ""
    try:
        extra += f'<script type="application/ld+json">{_ld(_store_schema())}</script>'
        extra += f'<script type="application/ld+json">{_ld({"@context": "https://schema.org", "@type": "WebSite", "name": SITE_NAME, "url": SITE_URL + "/"})}</script>'
    except Exception as e:
        print(f"[SEO] store schema failed: {e}")
    if GOOGLE_SITE_VERIFICATION:
        extra += f'<meta name="google-site-verification" content="{_e(GOOGLE_SITE_VERIFICATION)}">'
    return page.replace("<!--SEO_HEAD-->", extra, 1)


# ============================================================
#  AI assistant for Bake & Party   (POST /api/ai/chat)
# ============================================================
# Required on the server (Railway):
#   1. A variable named ANTHROPIC_API_KEY with a key from console.anthropic.com
#   2. (optional) AI_MODEL to choose another model. The default is a fast, low-cost model.
#   3. requirements.txt already includes the "anthropic" package.
# Without a key the endpoint doesn't crash: it returns a friendly message pointing to phone/WhatsApp.

AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5-20251001")
AI_MAX_PRODUCTS = 40          # How many matching products to include in the context
AI_MAX_MSG_CHARS = 1500       # Maximum length of a user message (abuse protection)

# ============================================================
#  Wolt stock check ("what went out of / back in stock"), admins only, in admin.html
# ============================================================
# An earlier version scraped the live Wolt page. It proved unreliable: Wolt loads part of the
# catalog only while scrolling (JavaScript), so plain HTML misses products and caused false
# "out of stock" reports.
#
# This version uses Wolt's own data: the same Excel export used for imports. Wolt marks
# every unavailable product with inventory_mode = forced_out_of_stock, so nothing is guessed.
#
#
# The comparison works both ways:
#   1. In stock here but forced_out_of_stock on Wolt
#      -> probably sold out, suggest marking it here too.
#   2. Out of stock here but available on Wolt
#      -> probably restocked, suggest bringing it back here too.

def _normalize_name_tokens(name):
    """Reduces a product name to meaningful words for comparison (drops numbers, units and punctuation)."""
    import re
    text = re.sub(r"[\d.,%]+\s*(g|kg|ml|l|יח['\"]?|גרם|מ\"ל|מ״ל|ליטר)?", " ", name, flags=re.IGNORECASE)
    text = re.sub(r"[^\u0590-\u05FFA-Za-z\s]", " ", text)
    return {t for t in text.split() if len(t) >= 2}


def _best_name_match(target_tokens, candidates_with_tokens):
    """Finds the best match (Jaccard) among (name, tokens) pairs. Returns (name, similarity) or (None, 0)."""
    best_name, best_sim = None, 0
    for cand_name, cand_tokens in candidates_with_tokens:
        if not cand_tokens or not target_tokens:
            continue
        sim = len(target_tokens & cand_tokens) / len(target_tokens | cand_tokens)
        if sim > best_sim:
            best_name, best_sim = cand_name, sim
    return best_name, best_sim


def compare_stock_with_wolt_file(file_path):
    """Compares a Wolt Excel export ('offers' sheet) with our products.

    "Unavailable on Wolt" means either inventory_mode == 'forced_out_of_stock' or enabled == False
    (a disabled product may not be marked out of stock but still can't be bought there).

    Returns a dict with three lists:
    - to_mark_out_of_stock: in stock here, unavailable on Wolt
    - to_mark_back_in_stock: out of stock here, available on Wolt
    - new_products_not_in_our_site: on Wolt but not in our Products table at all

    Raises ValueError with a clear (Hebrew, user-facing) message for the wrong kind of export:
    only the export with an "offers" sheet and an "inventory_mode" column has stock data.
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
        # pandas returns numpy.bool_, not a Python bool, so "is False" never matched a disabled product
        en = row.get("enabled") if has_enabled_col else None
        is_disabled = has_enabled_col and pd.notna(en) and str(en).strip().lower() in ("false", "0", "0.0")
        is_out = is_forced_out or is_disabled
        price = row.get("price")
        price = float(price) if pd.notna(price) else None
        # Wolt sometimes lists several comma-separated images; take the first
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
            # In stock here: does Wolt mark it as unavailable?
            match_name, sim = _best_name_match(our_tokens, wolt_out_of_stock)
            if sim >= SIM_THRESHOLD:
                # Make sure there isn't a better match among the available items (name collisions)
                avail_match, avail_sim = _best_name_match(our_tokens, wolt_available)
                if avail_sim < sim:
                    to_mark_out.append({
                        "our_id": prod_id, "our_name": prod_name,
                        "wolt_name": match_name, "similarity": round(sim, 2),
                    })
        else:
            # Out of stock here: does Wolt mark it as available?
            match_name, sim = _best_name_match(our_tokens, wolt_available)
            if sim >= SIM_THRESHOLD:
                out_match, out_sim = _best_name_match(our_tokens, wolt_out_of_stock)
                if out_sim < sim:
                    to_mark_back.append({
                        "our_id": prod_id, "our_name": prod_name,
                        "wolt_name": match_name, "similarity": round(sim, 2),
                    })

    # Products on Wolt with no match here at all (missing, not just out of stock)
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
                # Wolt charges 30% more. Site price = Wolt price / 1.3, rounded up to x.9 (same as the import)
                "site_price": _wolt_price(price, True, "nine") if price is not None else None,
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
    """Takes a Wolt Excel export and returns a two-way stock comparison.
    Read-only: it only returns suggestions; admins apply them from admin.html.
    """
    tmp_path = f"/tmp/wolt_check_{uuid.uuid4().hex}.xlsx"
    try:
        data = await file.read(15 * 1024 * 1024 + 1)
        if len(data) > 15 * 1024 * 1024:
            raise ValueError("הקובץ גדול מדי (עד 15MB)")
        with open(tmp_path, "wb") as f:
            f.write(data)
        result = compare_stock_with_wolt_file(tmp_path)
        return result
    except ValueError as e:
        # Clear validation errors (wrong file type); the message is already user-facing Hebrew
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
- הזמנות: אפשר להזמין דרך האתר לאיסוף עצמי מהחנות (מוסיפים לעגלה ושולחים). התשלום בחנות בזמן האיסוף, ומקבלים עדכון כשההזמנה מוכנה. משלוחים עדיין רק בתיאום טלפוני/וואטסאפ מעל 200 ₪.
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

# --- Chat abuse protection ---
AI_DAILY_LIMIT = int(os.environ.get("AI_DAILY_LIMIT", "30"))          # Max messages per user per day
AI_OFFTOPIC_BLOCK_THRESHOLD = 5                                       # Off-topic messages in a row before a block
AI_BLOCK_HOURS = 24                                                   # Block length in hours


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
    """Builds the context text: category structure + products matching the message + contact details."""
    parts = []
    try:
        conn = get_conn()
        cur = conn.cursor()

        # Store structure: main categories -> subcategories
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

        # Products matching words in the user's message. Search both the name and the description/notes,
        # so it also works when a customer describes a product in their own words
        # without remembering its exact name.
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

    # Contact details
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
    """Counts each chat question by its topic. Once a topic passes FAQ_AUTO_PUBLISH_THRESHOLD,
    its prepared question and answer are published on the home page. Errors here never break the chat.
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
    """Before calling the AI: is the user temporarily blocked (repeated off-topic messages)
    or over the daily limit? Returns (allowed: bool, message: str | None).
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
    """Runs after each successful message: counts it toward the daily limit and updates the
    off-topic streak (reset by an on-topic message), blocking temporarily past the threshold.
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
    """Saves a row in the chat log. Never breaks the chat if saving fails."""
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


def _parse_ai_reply(raw):
    """Extracts the reply from the structured {"reply","topic","on_topic"} format.
    The model doesn't always return clean JSON (text around it, line breaks inside strings, code fences),
    so several strategies are tried. Raw JSON is never shown to the customer."""
    import re
    raw = (raw or "").strip()
    text = re.sub(r"```(?:json)?", "", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    candidates = [text[start:end + 1]] if start != -1 and end > start else []
    candidates.append(text)
    for c in candidates:
        try:
            parsed = json.loads(c, strict=False)        # strict=False allows line breaks inside strings
            if isinstance(parsed, dict) and parsed.get("reply"):
                on_topic = parsed.get("on_topic")
                if isinstance(on_topic, str):
                    on_topic = {"true": True, "false": False}.get(on_topic.strip().lower())
                return (str(parsed.get("reply")).strip(), str(parsed.get("topic") or "").strip().lower() or None,
                        on_topic if isinstance(on_topic, bool) else None)
        except Exception:
            pass
    # Broken JSON: extract only the reply text
    m = re.search(r'"reply"\s*:\s*"(.*?)"\s*,\s*"(?:topic|on_topic)"', text, re.S) or re.search(r'"reply"\s*:\s*"(.*)', text, re.S)
    if m:
        val = m.group(1)
        val = re.sub(r'"\s*[,}]?\s*$', "", val)
        try:
            val = json.loads('"' + val.replace("\n", "\\n") + '"', strict=False)
        except Exception:
            val = val.replace('\\"', '"').replace("\\n", "\n")
        tm = re.search(r'"topic"\s*:\s*"([a-z_]+)"', text)
        om = re.search(r'"on_topic"\s*:\s*(true|false)', text)
        return val.strip(), (tm.group(1) if tm else None), ({"true": True, "false": False}[om.group(1)] if om else None)
    if text.startswith("{"):
        return "", None, None                            # Looks like JSON we can't read: better to show the default message
    return raw, None, None


@app.get("/api/admin/ai-log")
def admin_ai_log(limit: int = 200, only_failed: bool = False, sess: dict = Depends(require_admin)):
    """Latest AI chat questions (newest first), including who asked and the status."""
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

    # Block / daily limit check (never applies to admins)
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

    # Build the conversation (up to the last 8 messages of history)
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
            max_tokens=1000,
            system=system,
            messages=messages,
        )
        raw = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()

        # The model is asked for structured JSON {"reply", "topic", "on_topic"}. _parse_ai_reply handles
        # imperfect output; if the format can't be read, the message is answered without classification
        # rather than breaking the chat.
        reply, topic, on_topic = _parse_ai_reply(raw)

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


# --- Wolt API (OAuth connection + order webhook) ---
# Flow (per Wolt): the shop clicks "connect" -> Wolt's authorization page -> Wolt redirects back to
# /api/wolt/oauth/callback with a one-time code -> we exchange it for an access token (~1 hour)
# and a refresh token (~1 month, single use). Orders arrive as a small "ping" on /api/wolt/webhook/orders
# with the order id; the full order is then fetched from Wolt's API.
# The URLs of Wolt's own endpoints come from their documentation, so they are configured by env vars.
WOLT_CLIENT_ID = os.environ.get("WOLT_CLIENT_ID", "")
WOLT_CLIENT_SECRET = os.environ.get("WOLT_CLIENT_SECRET", "")
WOLT_AUTHORIZE_URL = os.environ.get("WOLT_AUTHORIZE_URL", "")
WOLT_TOKEN_URL = os.environ.get("WOLT_TOKEN_URL", "")
WOLT_SCOPE = os.environ.get("WOLT_SCOPE", "")
WOLT_WEBHOOK_SECRET = os.environ.get("WOLT_WEBHOOK_SECRET", "")
WOLT_REDIRECT_PATH = "/api/wolt/oauth/callback"
_wolt_webhook_hits = defaultdict(list)

def _wolt_redirect_uri():
    return SITE_URL.rstrip("/") + WOLT_REDIRECT_PATH

def _wolt_oauth_configured():
    return bool(WOLT_CLIENT_ID and WOLT_CLIENT_SECRET and WOLT_AUTHORIZE_URL and WOLT_TOKEN_URL)

def _wolt_token_request(fields):
    """POST to Wolt's token endpoint (standard OAuth 2.0 form body). Returns the JSON reply."""
    import urllib.request, urllib.error, urllib.parse
    data = urllib.parse.urlencode({**fields, "client_id": WOLT_CLIENT_ID, "client_secret": WOLT_CLIENT_SECRET}).encode()
    req = urllib.request.Request(WOLT_TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Wolt {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")

def _wolt_save_tokens(cur, tok, venue_id=None, extra=None):
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in") or 3600))
    venue = venue_id or tok.get("venue_id") or "default"
    cur.execute("""INSERT INTO wolt_connection (venue_id, access_token, refresh_token, expires_at, extra)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (venue_id) DO UPDATE SET access_token = EXCLUDED.access_token,
                       refresh_token = COALESCE(EXCLUDED.refresh_token, wolt_connection.refresh_token),
                       expires_at = EXCLUDED.expires_at, extra = COALESCE(EXCLUDED.extra, wolt_connection.extra),
                       updated_at = NOW();""",
                (venue, tok.get("access_token"), tok.get("refresh_token"), expires, json.dumps(extra) if extra else None))

def wolt_access_token(venue_id="default"):
    """A valid access token, refreshed when it is about to expire. The refresh token is single use,
    so the row is locked while refreshing: two parallel requests never spend the same refresh token."""
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT access_token, refresh_token, expires_at FROM wolt_connection WHERE venue_id = %s FOR UPDATE;", (venue_id,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError("החנות עוד לא מחוברת לוולט")
        access, refresh, expires = row
        if access and expires and expires - datetime.now(timezone.utc) > timedelta(minutes=5):
            return access
        tok = _wolt_token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        _wolt_save_tokens(cur, tok, venue_id)
        conn.commit()
        return tok["access_token"]
    finally:
        conn.rollback(); cur.close(); conn.close()

@app.get("/api/admin/wolt/status")
def wolt_status(sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT venue_id, expires_at, connected_at, updated_at FROM wolt_connection ORDER BY id;")
    venues = [{"venue_id": r[0], "token_expires": r[1].isoformat() if r[1] else None,
               "connected_at": r[2].isoformat() if r[2] else None} for r in cur.fetchall()]
    cur.execute("SELECT COUNT(*), MAX(received_at) FROM wolt_events;")
    n, last = cur.fetchone()
    cur.close(); conn.close()
    return {"configured": _wolt_oauth_configured(), "venues": venues,
            "redirect_url": _wolt_redirect_uri(), "webhook_url": SITE_URL.rstrip("/") + "/api/wolt/webhook/orders",
            "webhook_secret_set": bool(WOLT_WEBHOOK_SECRET), "events": n, "last_event": last.isoformat() if last else None}

@app.post("/api/admin/wolt/connect")
def wolt_connect(sess: dict = Depends(require_admin)):
    """Builds the link to Wolt's authorization page. 'state' ties the answer to this request (CSRF protection)."""
    import urllib.parse
    if not _wolt_oauth_configured():
        raise HTTPException(status_code=400, detail="חסרים פרטי החיבור מוולט (WOLT_CLIENT_ID וכו') ב-Railway")
    state = secrets.token_urlsafe(24)
    conn = get_conn(); cur = conn.cursor()
    _setting_set(cur, "wolt_oauth_state", json.dumps({"h": hashlib.sha256(state.encode()).hexdigest(),
                                                      "t": time_module.time(), "by": sess["user_id"]}))
    conn.commit(); cur.close(); conn.close()
    q = {"response_type": "code", "client_id": WOLT_CLIENT_ID, "redirect_uri": _wolt_redirect_uri(), "state": state}
    if WOLT_SCOPE:
        q["scope"] = WOLT_SCOPE
    sep = "&" if "?" in WOLT_AUTHORIZE_URL else "?"
    return {"url": WOLT_AUTHORIZE_URL + sep + urllib.parse.urlencode(q)}

def _wolt_result_page(ok, text):
    color = "#2e7d32" if ok else "#c62828"
    return HTMLResponse(f'<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>חיבור לוולט</title><body style="font-family:Arial,sans-serif;background:#FFF8F5;color:#4A2E35;text-align:center;padding:40px 16px">'
                        f'<h2 style="color:{color}">{"✓ " if ok else ""}{_html_escape(text)}</h2>'
                        f'<p><a href="/admin.html" style="color:#E05276">חזרה לפאנל הניהול</a></p></body></html>',
                        status_code=200 if ok else 400)

@app.get(WOLT_REDIRECT_PATH)
def wolt_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Wolt sends the shop back here after it approves (or declines) the connection."""
    if error:
        return _wolt_result_page(False, "החיבור לוולט בוטל או נדחה")
    conn = get_conn(); cur = conn.cursor()
    try:
        saved = json.loads(_setting_get(cur, "wolt_oauth_state") or "{}")
        good_state = (state and saved.get("h") and hmac.compare_digest(saved["h"], hashlib.sha256(state.encode()).hexdigest())
                      and time_module.time() - saved.get("t", 0) < 900)
        if not good_state or not code:
            return _wolt_result_page(False, "הקישור לא תקף או שפג תוקפו. מתחילים שוב מהכפתור בפאנל הניהול.")
        _setting_set(cur, "wolt_oauth_state", "")          # the state works once
        conn.commit()
        try:
            tok = _wolt_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": _wolt_redirect_uri()})
        except Exception as e:
            print(f"[WOLT] token exchange failed: {e}")
            return _wolt_result_page(False, "וולט לא אישרו את החיבור. נסו שוב בעוד כמה דקות.")
        extra = {k: v for k, v in request.query_params.items() if k not in ("code", "state")}
        venue = request.query_params.get("venue_id") or tok.get("venue_id")
        _wolt_save_tokens(cur, tok, venue, extra or None)
        conn.commit()
        _audit({"user_id": saved.get("by"), "name": None}, "wolt_connected", "wolt", None, {"venue": venue or "default"})
        return _wolt_result_page(True, "החנות חוברה לוולט בהצלחה")
    finally:
        cur.close(); conn.close()

def _wolt_signature_ok(request, raw):
    """Wolt may sign webhook calls with the secret we give them. Until their docs confirm the exact header,
    accept an HMAC-SHA256 of the body (hex or base64, any header) or the secret itself in a header."""
    if not WOLT_WEBHOOK_SECRET:
        return None                                  # no secret configured: accepted, marked unverified
    import base64
    mac = hmac.new(WOLT_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).digest()
    wanted = {mac.hex(), "sha256=" + mac.hex(), base64.b64encode(mac).decode(), WOLT_WEBHOOK_SECRET}
    for name, val in request.headers.items():
        if name.lower() in ("content-type", "content-length", "host", "user-agent", "accept", "accept-encoding", "connection"):
            continue
        for part in [val.strip()] + [p.strip() for p in val.replace(";", ",").split(",")]:
            if any(hmac.compare_digest(part, w) for w in wanted):
                return True
    return False

@app.post("/api/wolt/webhook/orders")
async def wolt_order_webhook(request: Request):
    """Order notifications from Wolt (statuses chosen with Wolt). Stored right away and answered fast."""
    ip = request.client.host if request.client else "?"
    _simple_rate_limit(_wolt_webhook_hits, ip, 600, 60, "Too many requests")
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload too large")
    verified = _wolt_signature_ok(request, raw)
    if verified is False:
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    def pick(d, *keys):
        for k in keys:
            cur_v = d
            for part in k.split("."):
                cur_v = cur_v.get(part) if isinstance(cur_v, dict) else None
            if cur_v not in (None, ""):
                return str(cur_v)[:120]
        return None
    order_id = pick(body, "order.id", "order_id", "id", "resource_id", "body.order.id")
    status = pick(body, "order.status", "status", "type", "event", "body.order.status")
    venue = pick(body, "order.venue_id", "venue_id", "venue.id", "body.order.venue_id")
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO wolt_events (order_id, status, venue_id, payload, verified) VALUES (%s,%s,%s,%s,%s);",
                (order_id, status, venue, raw.decode("utf-8", "replace")[:100_000], bool(verified)))
    conn.commit(); cur.close(); conn.close()
    return {"ok": True}

@app.get("/api/admin/wolt/events")
def wolt_events(limit: int = 50, sess: dict = Depends(require_admin)):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT id, order_id, status, venue_id, verified, received_at FROM wolt_events ORDER BY id DESC LIMIT %s;", (max(1, min(limit, 200)),))
    rows = [{"id": r[0], "order_id": r[1], "status": r[2], "venue_id": r[3], "verified": r[4], "at": r[5].isoformat()} for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# --- Backups ---
# Never exported: login sessions and one-time tokens (useless after a restore, and dangerous if leaked)
_BACKUP_SKIP_TABLES = {"sessions", "login_challenges", "password_resets", "wolt_connection"}
# Secrets removed from the export: 2FA keys (staff re-enroll after a restore) and the unsubscribe-link key
_BACKUP_SKIP_COLUMNS = {"users": {"totp_secret", "totp_pending", "backup_codes"}}
_BACKUP_SKIP_SETTINGS = {"unsub_secret"}
AUTO_BACKUP_DAYS = int(os.environ.get("AUTO_BACKUP_DAYS", "7") or 0)   # 0 turns the emailed backup off
_BACKUP_EMAIL_MAX = 9 * 1024 * 1024

def _build_backup():
    """All tables as JSON inside a zip. Returns (filename, bytes, table_counts)."""
    import io, zipfile
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name;")
    tables = [r[0] for r in cur.fetchall() if r[0] not in _BACKUP_SKIP_TABLES]
    data, counts = {}, {}
    for t in tables:
        cur.execute(f'SELECT * FROM "{t}";')   # names come from the catalog, not from the user
        cols = [d[0] for d in cur.description]
        skip = _BACKUP_SKIP_COLUMNS.get(t, set())
        rows = [{c: v for c, v in zip(cols, r) if c not in skip} for r in cur.fetchall()]
        if t == "settings":
            rows = [r for r in rows if r.get("key") not in _BACKUP_SKIP_SETTINGS]
        data[t] = rows; counts[t] = len(rows)
    cur.close(); conn.close()
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    doc = {"app": "bake-party", "format": 1, "created_at": now.isoformat(timespec="seconds"), "tables": data}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("backup.json", json.dumps(doc, ensure_ascii=False, default=str, indent=1))
    return f"bake-party-backup-{now:%Y-%m-%d}.zip", buf.getvalue(), counts

@app.get("/api/admin/backup")
def admin_backup(sess: dict = Depends(require_admin)):
    """Download a full backup of the database (admins only, audited)."""
    from fastapi.responses import Response
    name, blob, counts = _build_backup()
    _audit(sess, "backup_download", "backup", None, {"size_kb": len(blob) // 1024, "rows": sum(counts.values())})
    return Response(blob, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

def run_auto_backup(force=False):
    """Email a backup to every active admin once every AUTO_BACKUP_DAYS days."""
    if (AUTO_BACKUP_DAYS <= 0 and not force) or not _email_configured():
        return False
    conn = get_conn(); cur = conn.cursor()
    last = _setting_get(cur, "last_auto_backup")
    if not force and last:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(days=AUTO_BACKUP_DAYS):
                cur.close(); conn.close(); return False
        except ValueError:
            pass
    # Claim this run first, so two server instances don't both send
    _setting_set(cur, "last_auto_backup", datetime.now(timezone.utc).isoformat()); conn.commit()
    cur.execute("SELECT email FROM users WHERE role = 'admin' AND active AND email IS NOT NULL;")
    admins = [r[0] for r in cur.fetchall()]
    cur.close(); conn.close()
    name, blob, counts = _build_backup()
    total = sum(counts.values())
    lines = [f"מצורף גיבוי אוטומטי של כל נתוני האתר ({len(blob) // 1024} KB, {total} רשומות).",
             "כדאי לשמור את הקובץ במקום בטוח (למשל ב-Google Drive). אין צורך לפתוח אותו.",
             "בקובץ אין סיסמאות גלויות: הסיסמאות שמורות בו בצורה מוצפנת בלבד."]
    attach = [(name, blob)]
    if len(blob) > _BACKUP_EMAIL_MAX:
        lines = ["הגיבוי השבועי גדול מדי לשליחה במייל.", "אפשר להוריד אותו מפאנל הניהול: צוות ומשתמשים ← גיבוי."]
        attach = None
    for to in admins:
        _send_order_email(to, f"💾 גיבוי שבועי – Bake & Party", lines, SITE_URL.rstrip("/") + "/admin.html",
                          button="לפאנל הניהול", attachments=attach)
    return True

async def _backup_loop():
    import asyncio
    await asyncio.sleep(120)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, run_auto_backup)
        except Exception as e:
            print(f"[BACKUP] error: {e}")
        await asyncio.sleep(3600)


# --- Static files (HTML, logo, robots.txt) ---
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Stop browsers (especially on phones) from caching old HTML,
# so visitors see a new deploy right away without clearing the cache.
_NO_CACHE_HTML = {"Cache-Control": "no-cache, no-store, must-revalidate"}

@app.get("/")
def serve_index():
    return HTMLResponse(_render_index_html(), headers=_NO_CACHE_HTML)

@app.get("/admin.html")
def serve_admin():
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"), headers=_NO_CACHE_HTML)

@app.get("/staff.html")
def serve_staff():
    return FileResponse(os.path.join(STATIC_DIR, "staff.html"), headers=_NO_CACHE_HTML)

@app.get("/ui-inputs.js")
def serve_ui_inputs():
    return FileResponse(os.path.join(STATIC_DIR, "ui-inputs.js"), media_type="application/javascript", headers={"Cache-Control": "no-cache"})

@app.get("/logo-web.jpg")
def serve_logo_web():
    # Optimized logo (~65KB instead of 2MB) for the site itself
    return FileResponse(os.path.join(STATIC_DIR, "logo-web.jpg"), headers={"Cache-Control": "public, max-age=604800"})

@app.get("/logo.jpg")
def serve_logo():
    # Old links and social previews point here; serve the optimized logo
    return FileResponse(os.path.join(STATIC_DIR, "logo-web.jpg"), headers={"Cache-Control": "public, max-age=604800"})

@app.get("/privacy")
@app.get("/privacy.html")
def serve_privacy():
    """Privacy policy (also the link required by Google's sign-in consent screen)."""
    c = _get_contact()
    page = open(os.path.join(STATIC_DIR, "privacy.html"), encoding="utf-8").read()
    for key, val in (("SITE_URL", SITE_URL), ("ADDRESS", c["address"]), ("PHONE", c["phone"]), ("EMAIL", c["email"])):
        page = page.replace("{{" + key + "}}", _html_escape(val or ""))
    return HTMLResponse(page, headers=_NO_CACHE_HTML)

@app.get("/robots.txt")
def serve_robots():
    path = os.path.join(STATIC_DIR, "robots.txt")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404)