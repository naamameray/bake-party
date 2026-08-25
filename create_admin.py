"""
הקמת חשבונות והגדרות ל-Bake & Party + יצירת משתמש מנהל.

הרצה:  python create_admin.py

הסקריפט:
  1. יוצר את הטבלאות users ו-settings (אם אינן קיימות).
  2. מזין הגדרות ברירת מחדל לחנות (שעות פתיחה/סגירה, מצב משלוחים).
  3. יוצר / מעדכן משתמש מנהל שדרכו נכנסים לפאנל הניהול.

הטבלאות האלה נפרדות מ-Products/Categories, כך שהרצה חוזרת של import_data.py
(שמייבא מוצרים מחדש) לא תמחק את המשתמשים או ההגדרות.
"""

import getpass
import hashlib
import os
import sys

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": "5433",
    "dbname": "bakeshop",
    "user": "admin",
    "password": "adminpassword",
}


def hash_password(password, iterations=200_000):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def ensure_tables(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            SERIAL PRIMARY KEY,
            name          VARCHAR(150),
            email         VARCHAR(255) UNIQUE NOT NULL,
            phone         VARCHAR(50),
            address       TEXT,
            password_hash TEXT NOT NULL,
            role          VARCHAR(20) NOT NULL DEFAULT 'customer',  -- 'customer' | 'admin'
            created_at    TIMESTAMP DEFAULT NOW()
        );
        """
    )
    # תיקון טבלה קיימת שנוצרה בגרסה מוקדמת: מוודאים שהעמודות האופציונליות
    # (טלפון, כתובת, שם) יכולות להיות ריקות – אחרת יצירת מנהל ללא טלפון נכשלת.
    for column in ("phone", "address", "name"):
        cur.execute(f"ALTER TABLE users ALTER COLUMN {column} DROP NOT NULL;")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   VARCHAR(50) PRIMARY KEY,
            value TEXT
        );
        """
    )
    # הגדרות ברירת מחדל – רק אם עוד אין
    cur.execute(
        """
        INSERT INTO settings (key, value) VALUES
            ('opening_time', '09:00'),
            ('closing_time', '18:00'),
            ('delivery_override', 'auto')   -- auto = לפי שעות | on = פתוח תמיד | off = סגור
        ON CONFLICT (key) DO NOTHING;
        """
    )

    # --- טבלאות שעות פעילות (היו חסרות לגמרי!) ---
    # main.py ו-admin.html מסתמכים על שתי הטבלאות האלה (שעות שבועיות + ימים
    # מיוחדים/חגים), אבל שום סקריפט לא באמת יצר אותן. בלי זה, כל מה שקשור
    # לשעות פתיחה (סטטוס "פתוח/סגור", לוח השעות באתר, פאנל השעות ב-admin)
    # קורס עם שגיאת "relation does not exist".
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_hours (
            day_of_week   INTEGER PRIMARY KEY,  -- 0 = ראשון ... 6 = שבת (תואם ל-main.py)
            day_name      VARCHAR(20) NOT NULL,
            is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
            opening_time  VARCHAR(5) NOT NULL DEFAULT '09:00',
            closing_time  VARCHAR(5) NOT NULL DEFAULT '18:00'
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS special_days (
            id            SERIAL PRIMARY KEY,
            holiday_date  DATE UNIQUE NOT NULL,
            title         VARCHAR(255) NOT NULL,
            is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
            opening_time  VARCHAR(5) DEFAULT '09:00',
            closing_time  VARCHAR(5) DEFAULT '18:00',
            note          TEXT
        );
        """
    )
    # זריעת 7 ימי השבוע כברירת מחדל – רק אם הטבלה ריקה (לא דורסים שינויים קיימים)
    day_names = ["ראשון", "שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת"]
    for dow, name in enumerate(day_names):
        cur.execute(
            "INSERT INTO weekly_hours (day_of_week, day_name, is_closed, opening_time, closing_time) "
            "VALUES (%s, %s, FALSE, '09:00', '18:00') ON CONFLICT (day_of_week) DO NOTHING;",
            (dow, name),
        )


def main():
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    ensure_tables(cur)
    conn.commit()

    print("=== יצירת / איפוס משתמש מנהל ל-Bake & Party ===")
    email = input("אימייל של המנהל: ").strip().lower()
    if not email or "@" not in email:
        print("אימייל לא תקין.")
        sys.exit(1)
    name = input("שם המנהל: ").strip() or "מנהל"

    password = getpass.getpass("סיסמה: ")
    confirm = getpass.getpass("אימות סיסמה: ")
    if password != confirm:
        print("הסיסמאות אינן תואמות.")
        sys.exit(1)
    if len(password) < 6:
        print("הסיסמה חייבת להיות באורך 6 תווים לפחות.")
        sys.exit(1)

    cur.execute(
        """
        INSERT INTO users (name, email, password_hash, role)
        VALUES (%s, %s, %s, 'admin')
        ON CONFLICT (email) DO UPDATE
            SET password_hash = EXCLUDED.password_hash,
                role = 'admin',
                name = EXCLUDED.name;
        """,
        (name, email, hash_password(password)),
    )
    conn.commit()
    cur.close()
    conn.close()
    print(f"המנהל '{email}' נשמר בהצלחה. אפשר להתחבר דרך האתר או דרך admin.html 🎉")


if __name__ == "__main__":
    main()