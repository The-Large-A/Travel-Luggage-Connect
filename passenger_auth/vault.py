"""
Encryption at rest for the two mock external databases (mock UIDAI, mock TNSTC PNR).

Design (application-level, so it works the same on MongoDB Community, Atlas and
mongomock in the tests):

  * AES-256-GCM authenticated encryption (Python `cryptography`), a fresh random
    96-bit nonce per write, the whole sensitive part of a record sealed as one
    JSON blob: {"kid": key id, "ct": base64(nonce ‖ ciphertext ‖ tag)}.
  * Associated data binds every blob to its own record ("bookings|<pnr index>|<seq>"),
    so a ciphertext copied onto another record fails to decrypt.
  * Lookups use blind indexes — keyed HMAC-SHA256 of the identifier — never the
    identifier itself, so the database alone reveals no PNR or Aadhaar number.
  * Key separation: each store ("uidai", "pnr") gets its own encryption key and
    its own blind-index key, derived with HKDF-SHA256. The blind-index key comes
    from its OWN secret (DATA_INDEX_KEY), never from an encryption key, so
    encryption keys can be rotated without recomputing any index.
  * Keys come from DATA_ENCRYPTION_KEYS ("k1:<base64 32 bytes>,k2:...") with
    DATA_ENCRYPTION_KEY_ID naming the one used for new writes; older ids still
    decrypt, which is how keys are rotated (stores.reseal_all re-encrypts).
    Without them (development), keys are derived from SIGNING_SECRET and a
    warning is logged. Keys never live in the database.
"""
import base64
import hashlib
import hmac
import json
import logging
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

STORES = ("uidai", "pnr")
NONCE_BYTES = 12


class VaultError(Exception):
    """Decryption failed (wrong key, tampered or moved ciphertext) or bad key config."""


def _hkdf(master: bytes, info: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"tnstc-vault-v1", info=info.encode()).derive(master)


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class Vault:
    def __init__(self, masters: dict, current_kid: str, index_master: bytes):
        if current_kid not in masters:
            raise VaultError(f"DATA_ENCRYPTION_KEY_ID {current_kid!r} is not in DATA_ENCRYPTION_KEYS")
        for kid, key in masters.items():
            if len(key) != 32:
                raise VaultError(f"Encryption key {kid!r} must be 32 bytes (base64 of 32 random bytes)")
        self.current_kid = current_kid
        self._enc = {(store, kid): AESGCM(_hkdf(key, f"enc:{store}")) for kid, key in masters.items()
                     for store in STORES}
        if len(index_master) < 32:
            raise VaultError("DATA_INDEX_KEY must be at least 32 bytes")
        self._idx = {store: _hkdf(index_master, f"idx:{store}") for store in STORES}

    @classmethod
    def from_env(cls, signing_secret: str, environ=None) -> "Vault":
        env = os.environ if environ is None else environ
        def derived(label):
            return hmac.new(signing_secret.encode(), label, hashlib.sha256).digest()

        idx_raw = (env.get("DATA_INDEX_KEY") or "").strip()
        try:
            index_master = _b64d(idx_raw) if idx_raw else derived(b"tnstc-data-index-master-v1")
        except (ValueError, TypeError):
            raise VaultError("DATA_INDEX_KEY is not valid base64")
        raw = (env.get("DATA_ENCRYPTION_KEYS") or "").strip()
        if not raw:
            logger.warning("DATA_ENCRYPTION_KEYS is not set: deriving the data-encryption key from "
                           "SIGNING_SECRET (fine for development; set separate keys in production).")
            return cls({"dev": derived(b"tnstc-data-encryption-master-v1")}, "dev", index_master)
        masters = {}
        for part in raw.split(","):
            kid, _, b64 = part.strip().partition(":")
            try:
                masters[kid.strip()] = _b64d(b64.strip())
            except (ValueError, TypeError):
                raise VaultError(f"Encryption key {kid!r} is not valid base64")
        kid = (env.get("DATA_ENCRYPTION_KEY_ID") or sorted(masters)[-1]).strip()
        return cls(masters, kid, index_master)

    # ── blind index ──────────────────────────────────────────────────────────
    def index(self, store: str, value: str) -> str:
        """Keyed HMAC of an identifier: the only lookup form stored."""
        return hmac.new(self._idx[store], f"{store}|{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:40]

    # ── seal / open ──────────────────────────────────────────────────────────
    def seal(self, store: str, record: dict, aad: str) -> dict:
        nonce = os.urandom(NONCE_BYTES)
        plaintext = json.dumps(record, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ct = self._enc[(store, self.current_kid)].encrypt(nonce, plaintext, self._aad(aad, self.current_kid))
        return {"v": 1, "kid": self.current_kid, "ct": _b64e(nonce + ct)}

    @staticmethod
    def _aad(aad: str, kid: str) -> bytes:
        # record binding + format version + key id: a blob cannot be moved to another
        # record, replayed under another key id, or reinterpreted by a future format.
        return f"{aad}|v1|{kid}".encode("utf-8")

    def open(self, store: str, blob: dict, aad: str) -> dict:
        try:
            aes = self._enc[(store, blob["kid"])]
            raw = _b64d(blob["ct"])
            if blob.get("v", 1) != 1:
                raise VaultError(f"unknown {store} record format v{blob.get('v')}")
            data = aes.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], self._aad(aad, blob["kid"]))
            return json.loads(data.decode("utf-8"))
        except VaultError:
            raise
        except Exception as e:                      # InvalidTag, KeyError, bad base64/JSON
            raise VaultError(f"cannot decrypt {store} record: {type(e).__name__}") from None

    def needs_rotation(self, blob: dict) -> bool:
        return blob.get("kid") != self.current_kid


def new_key_b64() -> str:
    """Helper for operators: a fresh base64 key for DATA_ENCRYPTION_KEYS."""
    return _b64e(os.urandom(32))
