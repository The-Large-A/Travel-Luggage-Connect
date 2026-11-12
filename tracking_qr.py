"""
tracking_qr.py — encrypted QR codes for lost-report slips (Fernet).

Before: the loss slip's QR was a plain URL with the live tracking ID in it, so
any QR scanner revealed it. Now the QR carries a Fernet token (AES-128-CBC +
HMAC-SHA256, from the `cryptography` library) whose plaintext is

    {"t": tracking_id, "r": request_id, "ts": unix_seconds}

Only this server can read it; any change makes it invalid; and it expires 72
hours after it was issued (the slip page always prints a fresh one).

Key: QR_FERNET_KEY in .env (generate with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
). Without it, a key is derived from SIGNING_SECRET with a warning (development).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 72 * 3600
MAX_TOKEN_CHARS = 512


class QRError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason                 # "malformed" | "invalid" | "expired"


def _key(environ=None) -> bytes:
    env = os.environ if environ is None else environ
    raw = (env.get("QR_FERNET_KEY") or "").strip()
    if raw:
        try:
            Fernet(raw.encode())
            return raw.encode()
        except (ValueError, TypeError):
            raise ValueError("QR_FERNET_KEY is not a valid Fernet key (32 url-safe base64 bytes)")
    secret = (env.get("SIGNING_SECRET") or "").strip()
    if not secret:
        raise ValueError("Set QR_FERNET_KEY (or SIGNING_SECRET) to issue encrypted QR codes")
    logger.warning("QR_FERNET_KEY is not set: deriving the QR key from SIGNING_SECRET (development only).")
    digest = hmac.new(secret.encode(), b"tnstc-tracking-qr-fernet-v1", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)


class TrackingQR:
    def __init__(self, environ=None, clock=time.time):
        self.fernet = Fernet(_key(environ))
        self.clock = clock

    def issue(self, tracking_id: str, request_id: str) -> str:
        now = int(self.clock())
        body = json.dumps({"t": tracking_id, "r": request_id, "ts": now}, separators=(",", ":")).encode()
        return self.fernet.encrypt_at_time(body, now).decode("ascii")

    def read(self, token) -> dict:
        """{"t", "r", "ts"} of a valid, unexpired token; raises QRError otherwise."""
        if not isinstance(token, str) or not 10 <= len(token.strip()) <= MAX_TOKEN_CHARS:
            raise QRError("malformed")
        now = int(self.clock())
        try:
            body = self.fernet.decrypt_at_time(token.strip().encode("ascii"), MAX_AGE_SECONDS, now)
        except InvalidToken:
            # Fernet says "invalid" for both tampering and age; tell them apart without the TTL.
            try:
                self.fernet.decrypt(token.strip().encode("ascii"))       # signature only, no age check
            except (InvalidToken, ValueError, TypeError):
                raise QRError("invalid")
            raise QRError("expired")
        except (ValueError, UnicodeEncodeError):
            raise QRError("malformed")
        try:
            data = json.loads(body)
        except ValueError:
            raise QRError("invalid")
        if not isinstance(data, dict) or not all(k in data for k in ("t", "r", "ts")):
            raise QRError("invalid")
        if now - int(data["ts"]) > MAX_AGE_SECONDS:            # belt and braces: our own timestamp too
            raise QRError("expired")
        return data


__all__ = ["TrackingQR", "QRError", "MAX_AGE_SECONDS"]
