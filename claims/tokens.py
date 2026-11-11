"""
Pickup tokens: a human code (XXXX-XXXX) and an HMAC-signed QR payload.

The code is derived as HMAC(key, claimId | nonce) mapped onto a 32-symbol
alphabet without the look-alikes 0/O and 1/I. The nonce is 16 CSPRNG bytes
stored on the claim; only HMAC(key, code) is stored as `tokenHash`. The server
can therefore re-display the code to its owner, but a database dump alone cannot
produce a valid code. Reissuing draws a new nonce, which revokes the old code
and every QR that embeds it.

QR payload (v2): "TNSTC2.<base64url(nonce || AES-256-GCM(json) || tag)>".
The JSON {v, cid (claimId), iid (itemId), tag (qrTagId), exp (epoch seconds),
code} is ENCRYPTED and authenticated, so a photo of the QR reveals nothing:
not the claim, the item tag, the expiry or the pickup code. The PNR is not in
it at all. The key is its own purpose-derived subkey; the prefix is GCM
associated data. v1 ("TNSTC1.", signed but readable) is refused as "old_qr".
"""
import base64
import hashlib
import hmac
import json
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # 32 symbols: no 0/O, no 1/I
CODE_LEN = 8
PREFIX = "TNSTC2"
OLD_PREFIX = "TNSTC1"                            # signed-but-readable QR from before v2
_NONCE = 12
_MAX_QR = 1024
_CODE_RE = re.compile(rf"[{ALPHABET}]{{{CODE_LEN}}}")
TAG_PREFIX = "TAG-"


class TokenError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_nonce() -> str:
    return secrets.token_hex(16)


def derive_code(key: bytes, claim_id: str, nonce: str) -> str:
    digest = hmac.new(key, f"code|{claim_id}|{nonce}".encode(), hashlib.sha256).digest()
    return "".join(ALPHABET[b % len(ALPHABET)] for b in digest[:CODE_LEN])   # 256 % 32 == 0: unbiased


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def normalize_code(value) -> str:
    if not isinstance(value, str):
        raise TokenError("malformed")
    code = re.sub(r"[\s\-]", "", value).upper()
    if not _CODE_RE.fullmatch(code):
        raise TokenError("malformed")
    return code


def code_hash(key: bytes, code: str) -> str:
    return hmac.new(key, f"pickup|{code}".encode(), hashlib.sha256).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal_payload(key: bytes, payload: dict) -> str:
    """Encrypt + authenticate the QR payload (AES-256-GCM, random 96-bit nonce)."""
    if len(key) != 32:
        raise ValueError("the QR key must be 32 bytes")
    plain = json.dumps({**payload, "v": 2}, separators=(",", ":"), sort_keys=True).encode()
    nonce = secrets.token_bytes(_NONCE)
    return f"{PREFIX}.{_b64(nonce + AESGCM(key).encrypt(nonce, plain, PREFIX.encode()))}"


def open_payload(key: bytes, value) -> dict:
    """Decrypted payload. Raises TokenError('malformed' | 'bad_signature' | 'old_qr')."""
    if not isinstance(value, str) or len(value) > _MAX_QR:
        raise TokenError("malformed")
    text = value.strip()
    if text.startswith(OLD_PREFIX + "."):
        raise TokenError("old_qr")
    head, _, body = text.partition(".")
    if head != PREFIX or not body or "." in body:
        raise TokenError("malformed")
    try:
        blob = _unb64(body)
    except (ValueError, TypeError):
        raise TokenError("malformed")
    if len(blob) < _NONCE + 16 + 2:
        raise TokenError("malformed")
    try:
        plain = AESGCM(key).decrypt(blob[:_NONCE], blob[_NONCE:], PREFIX.encode())
    except InvalidTag:
        raise TokenError("bad_signature")              # altered, or not sealed with our key
    try:
        payload = json.loads(plain)
    except ValueError:
        raise TokenError("malformed")
    if not isinstance(payload, dict) or payload.get("v") != 2 or not isinstance(payload.get("exp"), int):
        raise TokenError("malformed")
    return payload


def is_qr_payload(value) -> bool:
    return isinstance(value, str) and value.strip().startswith((PREFIX + ".", OLD_PREFIX + "."))


def new_tag_id() -> str:
    return TAG_PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize_tag(value) -> str:
    """Accepts 'TAG-XXXXXXXX' or a scanned tag QR 'TNSTC-TAG:TAG-XXXXXXXX'."""
    if not isinstance(value, str):
        raise TokenError("malformed")
    text = value.strip().upper()
    if text.startswith("TNSTC-TAG:"):
        text = text[len("TNSTC-TAG:"):]
    if not re.fullmatch(rf"TAG-[{ALPHABET}]{{{CODE_LEN}}}", text):
        raise TokenError("malformed")
    return text
