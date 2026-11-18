"""Generates a VAPID key pair for Web Push. Run once on your own computer:  python tools/gen_vapid.py
Paste the two printed values into Railway > Variables. Keep the private key secret.
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