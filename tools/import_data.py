import glob
import sys
import os

import pandas as pd
import psycopg2

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5433"),
    "dbname": os.environ.get("DB_NAME", "bakeshop"),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "adminpassword"),
}

# --- Database schema, with a link table (multiple categories per product) and a notes column ---
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
    """Wolt sometimes lists several comma-separated images; take the first."""
    if pd.isna(raw):
        return None
    return str(raw).split(",")[0].strip() or None

def extract_leaf_ids(category_string):
    """Takes a category cell from the sheet (possibly comma-separated) and returns the id of each category."""
    if pd.isna(category_string):
        return []
    
    # Split on commas in case one Excel cell holds several categories
    raw_cats = str(category_string).split(",")
    leaf_ids = []
    for c in raw_cats:
        c = c.strip()
        if not c:
            continue
        # 'name::id' -> keep only the id; a bare 'id' is kept as is
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

    # ---- Step 1: child -> parent mapping from the subcategories column ----------------
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

    # ---- Step 2: insert categories ------------------------------------------------
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

    # ---- Step 3: insert products and their category links -------------------------
    print(f"מייבא {len(df_offers)} מוצרים...")
    inserted = 0
    for _, row in df_offers.iterrows():
        name = str(row["name"])
        price = float(row["price"]) if pd.notna(row["price"]) else 0.0

        # Every category id from the sheet (several may be comma-separated)
        wolt_cat_ids = extract_leaf_ids(row.get("category_id"))
        
        # Map to our own database ids
        db_cat_ids = []
        for wid in wolt_cat_ids:
            if wid in wolt_to_db and wolt_to_db[wid] not in db_cat_ids:
                db_cat_ids.append(wolt_to_db[wid])

        # Main category for the Products table (always the first one)
        primary_cat_id = db_cat_ids[0] if db_cat_ids else None

        stock = 0 if str(row["inventory_mode"]) == "forced_out_of_stock" else 100
        weight = int(row["weight_in_grams"]) if pd.notna(row["weight_in_grams"]) else None
        units = (
            int(row["number_of_units"])
            if "number_of_units" in row and pd.notna(row["number_of_units"])
            else None
        )
        image_url = first_image(row["images"])
        
        # Product description: Wolt stores it in the "description" column;
        # fall back to "notes" if that column is missing.
        if "description" in row and pd.notna(row["description"]):
            notes = str(row["description"])
        elif "notes" in row and pd.notna(row["notes"]):
            notes = str(row["notes"])
        else:
            notes = None

        cursor.execute(
            """
            INSERT INTO Products
                (name, category_id, price, stock_quantity, weight_grams, units_per_package, image_url, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """,
            (name, primary_cat_id, price, stock, weight, units, image_url, notes),
        )
        new_prod_id = cursor.fetchone()[0]

        # Link the product to every category it belongs to
        for cid in db_cat_ids:
            cursor.execute(
                "INSERT INTO product_categories (product_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
                (new_prod_id, cid)
            )

        inserted += 1

    # ---- Step 4: a representative image for each category --------------------------
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


def find_excel_file():
    """Finds the .xlsx file in the folder automatically, since Wolt export names change every time.
    A file can also be given explicitly: python tools/import_data.py "export.xlsx"
    """
    if len(sys.argv) > 1:
        return sys.argv[1]

    candidates = [f for f in glob.glob("*.xlsx") if not f.startswith("~$")]
    if not candidates:
        print("לא נמצא קובץ xlsx בתיקייה. ודאי שקובץ האקסל של וולט נמצא באותה תיקייה, "
              "או הריצי: python import_data.py \"שם_הקובץ.xlsx\"")
        sys.exit(1)
    if len(candidates) > 1:
        print("נמצאו כמה קבצי xlsx בתיקייה:")
        for c in candidates:
            print("  -", c)
        print("נא להריץ עם שם קובץ מפורש: python import_data.py \"שם_הקובץ.xlsx\"")
        sys.exit(1)
    return candidates[0]


if __name__ == "__main__":
    excel_file = find_excel_file()
    print(f"משתמשת בקובץ: {excel_file}")

    # --- Safety confirmation ---
    # This script is no longer part of day-to-day work (products are managed in the admin panel),
    # and it is destructive: it drops and rebuilds Products and Categories, so every manual change
    # (prices, categories, images, deleted products) is replaced with raw Wolt data.
    # The explicit confirmation prevents running it by accident. For safe, additive imports
    # use the Wolt import in the admin panel instead.
    print()
    print("⚠️  שימו לב: הרצה זו תמחק ותבנה מחדש את טבלאות Products ו-Categories.")
    print("   כל שינוי ידני שנעשה דרך פאנל הניהול (מחירים, קטגוריות, תמונות,")
    print("   מוצרים שנמחקו) יימחק ויוחלף בנתונים הגולמיים מהאקסל של וולט.")
    confirm = input("   להמשיך בכל זאת? הקלידו בדיוק: כן / yes  ").strip().lower()
    if confirm not in ("כן", "yes", "y"):
        print("בוטל. שום דבר לא נמחק.")
        sys.exit(0)

    import_data(excel_file)