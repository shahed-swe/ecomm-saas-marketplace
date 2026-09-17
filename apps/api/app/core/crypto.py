"""PII protection (ADR 0009/0010): encrypted values for display to authorised staff, keyed hashes
for duplicate detection. Production swaps `Encryptor` for Vault transit; the interface stays."""

import base64
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

VERSION = "v1"


class Encryptor:
    def __init__(self, key_b64: str):
        key = base64.urlsafe_b64decode(key_b64)
        if len(key) != 32:
            raise ValueError("data_encryption_key must decode to 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, data: dict, *, context: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, json.dumps(data).encode(), context.encode())
        return f"{VERSION}:{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, *, context: str) -> dict:
        version, _, payload = token.partition(":")
        if version != VERSION:
            raise ValueError("unknown key version")
        raw = base64.urlsafe_b64decode(payload)
        return json.loads(self._aead.decrypt(raw[:12], raw[12:], context.encode()))


def keyed_hash(settings: Settings, tenant_id: str, value: str) -> str:
    norm = "".join(value.split()).lower()
    return hmac.new(
        settings.hash_key.encode(), f"{tenant_id}|{norm}".encode(), hashlib.sha256
    ).hexdigest()


def last4(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isalnum())
    return digits[-4:]
