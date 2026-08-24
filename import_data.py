import pandas as pd
import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": "5433",
    "dbname": "bakeshop",
    "user": "admin",
    "password": "adminpassword",
}

# --- סכימת מסד הנתונים מעודכנת לתמיכה בטבלת קישור (קטגוריות מרובות) ועמודת הערות ---
SCHEMA = """
DROP TABLE IF EXISTS product_categories CASCADE;
DROP TABLE IF EXISTS Products CASCADE;
DROP TABLE IF EXISTS Categories CASCADE;

CREATE TABLE Categories (
    id          SERIAL PRIMARY KEY,
    wolt_id     VARCHAR(64) UNIQUE,
    name        VARCHAR(255) NOT NULL,
    parent_id   INTEGER REFERENCES Categories(id) ON DELETE SET NULL,
    image_url   TEXT,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE Products (
    id                 SERIAL PRIMARY KEY,
    name               VARCHAR(500) NOT NULL,
    category_id        INTEGER REFERENCES Categories(id) ON DELETE SET NULL,
    price              NUMERIC(10,2) NOT NULL DEFAULT 0,
    stock_quantity     INTEGER NOT NULL DEFAULT 0,
    weight_grams       INTEGER,
    units_per_package  INTEGER,
    image_url          TEXT,
    notes              TEXT
);

CREATE TABLE product_categories (
    product_id INTEGER REFERENCES Products(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES Categories(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, category_id)
);

CREATE INDEX idx_products_category ON Products(category_id);
CREATE INDEX idx_categories_parent ON Categories(parent_id);
"""

def first_image(raw):
    """וולט לפעמים מחזיר כמה תמונות מופרדות בפסיק – לוקחים את הראשונה."""
    if pd.isna(raw):
        return None
    return str(raw).split(",")[0].strip() or None

def extract_leaf_ids(category_string):
    """
    מקבל מחרוזת של קטגוריות מהאקסל (שיכולות להיות מופרדות בפסיק)
    ומחלץ את ה-ID של כל אחת מהן.
    """
    if pd.isna(category_string):
        return []
    
    # מפצל לפי פסיקים במקרה שהכנסת כמה קטגוריות באותו תא באקסל
    raw_cats = str(category_string).split(",")
    leaf_ids = []
    for c in raw_cats:
        c = c.strip()
        if not c:
            continue
        # אם זה מגיע בפורמט 'שם::id', ניקח רק את ה-id. אם זה רק 'id', זה ייקח אותו.
        leaf_id = c.split("::")[-1].strip()
        if leaf_id:
            leaf_ids.append(leaf_id)
            
    return leaf_ids

def import_data(file_path):
    print("מתחבר למסד הנתונים...")
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    print("בונה מחדש את הטבלאות (כולל תמיכה בקטגוריות מרובות והערות)...")
    cursor.execute(SCHEMA)

    print("קורא את קובץ האקסל...")
    df_categories = pd.read_excel(file_path, sheet_name="categories")
    df_offers = pd.read_excel(file_path, sheet_name="offers")

    # ---- שלב 1: מיפוי ילד -> הורה מתוך עמודת subcategories -------------------
    child_to_parent = {}          
    order_within_parent = {}      
    for _, row in df_categories.iterrows():
        if pd.notna(row["subcategories"]):
            subs = [s.strip() for s in str(row["subcategories"]).split(",")]
            for i, sub in enumerate(subs):
                child_to_parent[sub] = str(row["id"])
                order_within_parent[sub] = i

    all_ids = [str(x) for x in df_categories["id"]]
    top_level_ids = [cid for cid in all_ids if cid not in child_to_parent]
    id_to_name = dict(zip(df_categories["id"].astype(str), df_categories["name"].astype(str)))

    # ---- שלב 2: הכנסת קטגוריות -----------------------------------------------
    print("מייבא קטגוריות (ראשיות + תתי-קטגוריות)...")
    wolt_to_db = {}   

    def insert_category(wolt_id, parent_db_id, sort_order):
        cursor.execute(
            "INSERT INTO Categories (wolt_id, name, parent_id, sort_order) "
            "VALUES (%s, %s, %s, %s) RETURNING id;",
            (wolt_id, id_to_name.get(wolt_id, "ללא שם"), parent_db_id, sort_order),
        )
        wolt_to_db[wolt_id] = cursor.fetchone()[0]

    for i, wid in enumerate(top_level_ids):
        insert_category(wid, None, i)

    remaining = [c for c in all_ids if c not in wolt_to_db]
    while remaining:
        progressed = False
        still = []
        for wid in remaining:
            parent_wolt = child_to_parent.get(wid)
            if parent_wolt in wolt_to_db:
                insert_category(wid, wolt_to_db[parent_wolt], order_within_parent.get(wid, 0))
                progressed = True
            else:
                still.append(wid)
        remaining = still
        if not progressed:   
            for wid in remaining:
                insert_category(wid, None, 999)
            break

    # ---- שלב 3: הכנסת מוצרים לטבלה הראשית ולטבלת הקישור ----------------------
    print(f"מייבא {len(df_offers)} מוצרים...")
    inserted = 0
    for _, row in df_offers.iterrows():
        name = str(row["name"])
        price = float(row["price"]) if pd.notna(row["price"]) else 0.0

        # חילוץ כל מזהי הקטגוריות מהאקסל (תומך במספר קטגוריות מופרדות בפסיק)
        wolt_cat_ids = extract_leaf_ids(row.get("category_id"))
        
        # המרה למזהים של מסד הנתונים שלנו
        db_cat_ids = []
        for wid in wolt_cat_ids:
            if wid in wolt_to_db and wolt_to_db[wid] not in db_cat_ids:
                db_cat_ids.append(wolt_to_db[wid])

        # קטגוריה ראשית לטבלת Products (תמיד הראשונה ברשימה)
        primary_cat_id = db_cat_ids[0] if db_cat_ids else None

        stock = 0 if str(row["inventory_mode"]) == "forced_out_of_stock" else 100
        weight = int(row["weight_in_grams"]) if pd.notna(row["weight_in_grams"]) else None
        units = (
            int(row["number_of_units"])
            if "number_of_units" in row and pd.notna(row["number_of_units"])
            else None
        )
        image_url = first_image(row["images"])
        
        # משיכת הערות מהאקסל אם קיימת העמודה "notes"
        notes = str(row["notes"]) if "notes" in row and pd.notna(row["notes"]) else None

        cursor.execute(
            """
            INSERT INTO Products
                (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """,
            (name, primary_cat_id, price, stock, weight, units, image_url, notes),
        )
        new_prod_id = cursor.fetchone()[0]

        # שמירה בטבלת הקשר עבור כל הקטגוריות שאליהן המוצר משויך
        for cid in db_cat_ids:
            cursor.execute(
                "INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
                (new_prod_id, cid)
            )

        inserted += 1

    # ---- שלב 4: תמונת נציג לכל קטגוריה --------------------------------------
    print("מגדיר תמונות נציגות לקטגוריות...")
    cursor.execute(
        """
        UPDATE Categories c
        SET image_url = sub.img
        FROM (
            SELECT DISTINCT ON (category_id) category_id, image_url AS img
            FROM Products
            WHERE image_url IS NOT NULL AND category_id IS NOT NULL
            ORDER BY category_id, id
        ) sub
        WHERE c.id = sub.category_id AND c.image_url IS NULL;
        """
    )
    cursor.execute(
        """
        UPDATE Categories p
        SET image_url = ch.img
        FROM (
            SELECT DISTINCT ON (parent_id) parent_id, image_url AS img
            FROM Categories
            WHERE image_url IS NOT NULL AND parent_id IS NOT NULL
            ORDER BY parent_id, sort_order, id
        ) ch
        WHERE p.id = ch.parent_id AND p.image_url IS NULL;
        """
    )

    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM Categories WHERE parent_id IS NULL;")
    n_main = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM Categories WHERE parent_id IS NOT NULL;")
    n_sub = cursor.fetchone()[0]

    cursor.close()
    conn.close()
    print(
        f"ייבוא הנתונים הסתיים בהצלחה! 🎉  "
        f"({n_main} קטגוריות ראשיות, {n_sub} תתי-קטגוריות, {inserted} מוצרים)"
    )


if __name__ == "__main__":
    # !!! שימי לב להחליף את זה לשם של קובץ האקסל המקורי שלך !!!
    import_data("[pinookim-givat-shmuel]-[2026-08-24]-[09_38].xlsx")