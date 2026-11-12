"""יוצר זוג מפתחות להתראות (VAPID). מריצים פעם אחת במחשב שלך:  python tools/gen_vapid.py
את שני הערכים שמודפסים מדביקים ב-Railway בלשונית Variables (לא שולחים אותם לאף אחד, והפרטי נשאר סודי).
"""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

key = ec.generate_private_key(ec.SECP256R1())
priv = key.private_numbers().private_value.to_bytes(32, "big")
pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
print("VAPID_PUBLIC_KEY=" + b64(pub))
print("VAPID_PRIVATE_KEY=" + b64(priv))