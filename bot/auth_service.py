"""
Web login credential hashing and verification.
Uses PBKDF2-SHA256 via stdlib - no extra dependencies.
Format: "pbkdf2$<salt_hex>$<hash_hex>"
"""

import hashlib
import secrets
import hmac
import base64
import json
import time

import string

ITERATIONS = 260_000
PASSWORD_LENGTH = 12

def generate_pin() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2${salt}${key.hex()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, salt_hex, hash_hex = stored.split("$")
        key = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt_hex), ITERATIONS)
        return secrets.compare_digest(key.hex(), hash_hex)
    except Exception:
        return False

def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')

def _b64_decode(data: str) -> bytes:
    padding = 4 - (len(data) % 4)
    if padding != 4:
        data += '=' * padding
    return base64.urlsafe_b64decode(data)

def create_jwt(payload: dict, secret: str, expires_in_seconds: int = 86400 * 7) -> str:
    """Create a minimal JWT signed with HMAC-SHA256."""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = payload.copy()
    payload["exp"] = int(time.time()) + expires_in_seconds
    
    encoded_header = _b64_encode(json.dumps(header).encode('utf-8'))
    encoded_payload = _b64_encode(json.dumps(payload).encode('utf-8'))
    
    msg = f"{encoded_header}.{encoded_payload}"
    signature = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).digest()
    
    return f"{msg}.{_b64_encode(signature)}"

def verify_jwt(token: str, secret: str) -> dict | None:
    """Verify JWT signature and expiration. Returns payload if valid, None if invalid or expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
            
        encoded_header, encoded_payload, encoded_signature = parts
        msg = f"{encoded_header}.{encoded_payload}"
        
        expected_signature = hmac.new(secret.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).digest()
        actual_signature = _b64_decode(encoded_signature)
        
        if not hmac.compare_digest(expected_signature, actual_signature):
            return None
            
        payload = json.loads(_b64_decode(encoded_payload).decode('utf-8'))
        
        if payload.get("exp", 0) < time.time():
            return None
            
        return payload
    except Exception:
        return None
