"""
Mock Aadhaar identity (Phase 2, Feature A) — a SIMULATION of UIDAI for this project.

Real Aadhaar authentication needs AUA/KUA licensing or approval under the 2025
Good Governance rules, so this module stands in for UIDAI with a small seeded
collection `mock_uidai`. It follows the real format rules:

  * Aadhaar number: 12 digits, first digit 2-9, last digit a Verhoeff checksum;
  * VID (Virtual ID): 16 digits, last digit a Verhoeff checksum, revocable, and
    the Aadhaar number cannot be derived from it.

Data minimisation: the application NEVER stores an Aadhaar number or VID. Each
is turned into a keyed HMAC token (key derived from SIGNING_SECRET) plus the
last 4 digits for display ("XXXX XXXX 1234"). A plain salted SHA-256 would not
do: 12 Verhoeff-valid digits are only ~10^11 values, brute-forceable once a
salt leaks; an HMAC key that never touches the database is not.

Encryption at rest: the mock UIDAI collection keeps only the two tokens in
clear (they are the lookup keys); name, linked phone, last 4 digits, year of
birth, gender and status are sealed with AES-256-GCM (stores.CitizenStore).
"""
import hashlib
import hmac
import re
from dataclasses import dataclass

from .otp import derive_key

MOCK_UIDAI = "mock_uidai"
TOKEN_PURPOSE = "tnstc-aadhaar-token-v1"

# ── Verhoeff checksum ────────────────────────────────────────────────────────
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5), (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7), (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3), (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4), (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7), (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(body: str) -> str:
    """The digit to append to `body` so the whole number is Verhoeff-valid."""
    c = 0
    for i, ch in enumerate(reversed(body)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


# ── parsing ──────────────────────────────────────────────────────────────────
AADHAAR, VID = "aadhaar", "vid"
_AADHAAR_RE = re.compile(r"[2-9][0-9]{11}")
_VID_RE = re.compile(r"[0-9]{16}")


class IdNumberError(ValueError):
    pass


def parse_id_number(raw) -> tuple:
    """
    Accept an Aadhaar number (12 digits) or VID (16 digits), spaces/dashes allowed.
    Returns (kind, digits). Raises IdNumberError on a bad format or checksum, so a
    typo is caught before any OTP is sent.
    """
    if not isinstance(raw, str):
        raise IdNumberError("ID number must be text")
    digits = re.sub(r"[\s-]", "", raw)
    if not digits.isdigit() or not digits.isascii():
        raise IdNumberError("Use digits only")
    if len(digits) == 12:
        if not _AADHAAR_RE.fullmatch(digits):
            raise IdNumberError("An Aadhaar number cannot start with 0 or 1")
        kind = AADHAAR
    elif len(digits) == 16 and _VID_RE.fullmatch(digits):
        kind = VID
    else:
        raise IdNumberError("Enter a 12-digit Aadhaar number or a 16-digit VID")
    if not verhoeff_valid(digits):
        raise IdNumberError("This number fails the Aadhaar checksum; please re-check it")
    return kind, digits


def mask_id(last4: str, kind: str = AADHAAR) -> str:
    return ("XXXX XXXX XXXX " if kind == VID else "XXXX XXXX ") + (last4 or "????")


# ── tokens ───────────────────────────────────────────────────────────────────
def token_key(signing_secret: str) -> bytes:
    return derive_key(signing_secret, TOKEN_PURPOSE)


def id_token(key: bytes, kind: str, digits: str) -> str:
    """Keyed, purpose-separated token: the only stored form of an ID number."""
    return hmac.new(key, f"{kind}:{digits}".encode("ascii"), hashlib.sha256).hexdigest()[:40]


@dataclass(frozen=True)
class Citizen:
    """A mock UIDAI record as the application sees it (no ID number inside)."""
    aadhaar_token: str
    last4: str
    name: str
    linked_phone: str
    year_of_birth: int
    gender: str


def find_citizen(citizens, key: bytes, kind: str, digits: str):
    """Look up an active citizen in the encrypted mock UIDAI store."""
    field = "aadhaarToken" if kind == AADHAAR else "vidToken"
    rec = citizens.by_token(field, id_token(key, kind, digits))
    if not rec:
        return None
    return Citizen(rec["aadhaarToken"], rec["last4"], rec["name"], rec["linkedPhone"],
                   rec.get("yearOfBirth", 0), rec.get("gender", ""))


# ── seed data ────────────────────────────────────────────────────────────────
def _number(body: str) -> str:
    return body + verhoeff_check_digit(body)


# Fictitious, Verhoeff-valid demo numbers matching the sample bookings in
# seed.py. (aadhaar, vid, name, year of birth, gender, linked phone)
MOCK_CITIZENS = (
    (_number("23456789012"), _number("912345678901234"), "Arun Kumar", 1994, "M", "+919876543210"),
    (_number("34567890123"), _number("923456789012345"), "Priya Raman", 1998, "F", "+919444012345"),
    (_number("45678901234"), _number("934567890123456"), "கார்த்திக் செல்வம்", 1990, "M", "+919840011223"),
    (_number("56789012345"), _number("945678901234567"), "Lakshmi Narayanan", 1985, "F", "+919003312345"),
    (_number("67890123456"), _number("956789012345678"), "Senthil Kumar", 1979, "M", "+919123456780"),
    (_number("78901234567"), _number("967890123456789"), "Meena Senthil", 1982, "F", "+919123456780"),
    (_number("89012345678"), _number("978901234567890"), "Ravi Senthil", 2004, "M", "+918098765432"),
    (_number("90123456789"), _number("989012345678901"), "Kavitha Mani", 1991, "F", "+917010203040"),
    (_number("61234567890"), _number("991234567890123"), "Divya Prakash", 1996, "F", "+919500011122"),
)


def mock_number_for(name: str) -> str:
    """Test/demo helper: the mock Aadhaar number of a seeded citizen."""
    for aadhaar, _vid, n, *_ in MOCK_CITIZENS:
        if n == name:
            return aadhaar
    raise KeyError(name)


def mock_vid_for(name: str) -> str:
    for _aadhaar, vid, n, *_ in MOCK_CITIZENS:
        if n == name:
            return vid
    raise KeyError(name)


SEALED_SCHEMA = {"bsonType": "object", "required": ["v", "kid", "ct"],
                 "properties": {"v": {"enum": [1]}, "kid": {"bsonType": "string"}, "ct": {"bsonType": "string"}}}

# The database itself refuses a clear-text record: only the tokens and the sealed blob may exist.
MOCK_UIDAI_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["aadhaarToken", "vidToken", "sealed"],
        "additionalProperties": False,
        "properties": {
            "_id":          {},
            "aadhaarToken": {"bsonType": "string"},
            "vidToken":     {"bsonType": "string"},
            "sealed":       SEALED_SCHEMA,
        },
    }
}


def seed_mock_uidai(db, signing_secret: str, vault=None) -> int:
    """Upsert the mock UIDAI records, encrypted. Idempotent."""
    from .stores import CitizenStore
    from .vault import Vault
    key = token_key(signing_secret)
    store = CitizenStore(db, vault or Vault.from_env(signing_secret))
    store.migrate_plaintext()
    for aadhaar, vid, name, yob, gender, phone in MOCK_CITIZENS:
        store.upsert(id_token(key, AADHAAR, aadhaar), id_token(key, VID, vid),
                     {"last4": aadhaar[-4:], "name": name, "linkedPhone": phone, "yearOfBirth": yob,
                      "gender": gender, "status": "active"})
    return len(MOCK_CITIZENS)


def booking_id_proof(signing_secret: str, name: str) -> dict:
    """The idProof a TNSTC booking would carry for a mock citizen (type, token, last 4)."""
    aadhaar = mock_number_for(name)
    return {"type": AADHAAR, "token": id_token(token_key(signing_secret), AADHAAR, aadhaar),
            "last4": aadhaar[-4:]}
