"""Reading supplier invoices with Claude, and the shop's pricing rules.

An invoice (PDF, typed or scanned, or a phone photo) is sent to Claude, which returns
its line items as JSON that follows a fixed schema. Nothing here touches the database:
main.py matches the lines to products and shows them for approval before anything changes.
"""
import base64
import io
import json
import math
import os

INVOICE_AI_MODEL = os.environ.get("INVOICE_AI_MODEL", "claude-opus-5-5")
MAX_INVOICE_BYTES = 20 * 1024 * 1024


# --- Pricing -------------------------------------------------------------------------------

def nice_round_up(v):
    """Round UP to the nearest price that ends in .00, .50 or .90 (12.10 -> 12.50, 12.60 -> 12.90, 12.95 -> 13.00)."""
    v = round(float(v), 4)
    n = math.floor(v + 1e-9)
    for c in (n, n + 0.5, n + 0.9, n + 1):
        if c >= v - 1e-9:
            return round(c, 2)
    return round(n + 1, 2)


def site_price_from_cost(cost):
    """The shop's rule: cost x 2 + 1.90, rounded up to .00 / .50 / .90."""
    return nice_round_up(float(cost) * 2 + 1.90)


def wolt_price_from_site(price):
    """Wolt's price: the site price + 30%, rounded up the same way."""
    return nice_round_up(float(price) * 1.3)


# --- Extraction ----------------------------------------------------------------------------

def _nullable(t):
    return {"anyOf": [{"type": t}, {"type": "null"}]}


INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier": _nullable("string"),
        "invoice_number": _nullable("string"),
        "invoice_date": _nullable("string"),
        "invoice_discount_percent": _nullable("number"),
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "barcode": _nullable("string"),
                    "supplier_sku": _nullable("string"),
                    "quantity": _nullable("number"),
                    "units_per_pack": _nullable("integer"),
                    "unit_cost": _nullable("number"),
                    "line_discount_percent": _nullable("number"),
                    "line_total": _nullable("number"),
                    "suggested_name": {"type": "string"},
                    "suggested_category_id": _nullable("integer"),
                    "suggested_description": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": _nullable("string"),
                },
                "required": ["description", "barcode", "supplier_sku", "quantity", "units_per_pack", "unit_cost", "line_discount_percent",
                             "line_total", "suggested_name", "suggested_category_id", "suggested_description",
                             "confidence", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["supplier", "invoice_number", "invoice_date", "invoice_discount_percent", "lines"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You read supplier invoices for Bake & Party, a baking and party supply shop in Israel.
Invoices are usually in Hebrew (right to left). Most arrive as phone photos or scans of paper, sometimes
tilted, sometimes several pages. Typical columns: item code (מק"ט / מפתח פריט / מס' פריט), barcode (ברקוד),
description (תיאור / שם פריט), quantity (כמות), price (מחיר), discount % (הנחה), line total (סה"כ).

Return every product line on the invoice, across all pages. A page may repeat the invoice header: read its
lines once. If the same page was photographed twice, do not list its lines twice. Skip lines that are not
products (delivery, deposits, totals, VAT).

Invoice level:
- invoice_discount_percent: a discount on the whole invoice (e.g. "הנחה 3.05%" in the totals box), otherwise null.
  For information only: it is NOT taken off unit_cost.

For each product line:
- description: the line text exactly as written.
- barcode: the barcode digits exactly as printed, KEEPING leading zeros (e.g. "0693493056403"). null if none. Never invent one.
- supplier_sku: the supplier's item code if printed, otherwise null.
- quantity: the quantity as written.
- units_per_pack: if the line is a carton/pack and the number of single units inside is stated, that number; otherwise null.
- unit_cost: the LIST price of ONE unit as printed in the price column (מחיר), WITHOUT VAT, BEFORE any
  discount. The shop prices from the list price, so do not take off the line discount or the invoice discount.
  Example: price 25.80, discount 5%, total 294.12, quantity 12 -> unit_cost 25.80.
  If the price column is missing, work it out as line_total / quantity / (1 - line_discount_percent / 100).
  If the numbers can't be read, null.
- line_discount_percent: the discount % printed on the line (הנחה), or null if none.
- line_total: the line total as printed, or null.
- suggested_name: a clean, short Hebrew product name for the shop's website: product + size/weight/colour,
  keeping kashrut words that matter to customers (חלבי, פרווה). Drop codes and abbreviations such as (ח.א), **, item numbers.
  Keep balloon sizes in inches (18").
- suggested_category_id: the best matching id from the category list below, or null if none fits.
- suggested_description: one or two plain Hebrew sentences for customers, using only what the invoice says
  (type, size, weight, flavour, colour, חלבי/פרווה). Never invent ingredients, allergens or claims.
- confidence: "low" when hard to read or a number is uncertain, "medium" when partly unsure, else "high".
- note: a short Hebrew note when a person should check something, otherwise null. Always add a note when the
  line is sold by the hundred or in bulk (e.g. "/100 יח'") and the unit cost is very small, because the shop
  may sell a bag rather than single units: say what the cost would be per bag.

Numbers must be plain numbers (12.5, not "12,50 ₪")."""


def _image_block(data):
    """Phone photos are shrunk and re-encoded as JPEG so they stay within the API's image limits."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((2400, 2400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def file_block(data, kind):
    if kind == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.standard_b64encode(data).decode()}}
    return _image_block(data)


def extract_invoice(data, kind, categories, api_key=None):
    """data: file bytes; kind: "pdf" or "image"; categories: [(id, "parent > name")].
    Returns the parsed dict (see INVOICE_SCHEMA). Raises RuntimeError with a Hebrew message on failure."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    cat_text = "\n".join(f"{cid}: {name}" for cid, name in categories)
    try:
        with client.beta.messages.stream(
            model=INVOICE_AI_MODEL,
            max_tokens=32000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": INVOICE_SCHEMA}},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                file_block(data, kind),
                {"type": "text", "text": "Category list (id: name):\n" + cat_text + "\n\nRead this invoice."},
            ]}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise RuntimeError("מפתח ה-AI לא תקין (ANTHROPIC_API_KEY)")
    except anthropic.RateLimitError:
        raise RuntimeError("יותר מדי בקשות ל-AI כרגע. נסו שוב בעוד דקה.")
    except anthropic.BadRequestError as e:
        raise RuntimeError(f"ה-AI לא הצליח לקרוא את הקובץ: {str(e.message)[:160]}")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"שגיאה בשירות ה-AI ({e.status_code}). נסו שוב.")
    except anthropic.APIConnectionError:
        raise RuntimeError("אין חיבור לשירות ה-AI. נסו שוב.")
    if msg.stop_reason == "refusal":
        raise RuntimeError("ה-AI סירב לקרוא את הקובץ")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("החשבונית ארוכה מדי לקריאה אחת. אפשר לפצל אותה לכמה קבצים.")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError("ה-AI החזיר תשובה לא תקינה. נסו שוב.")
