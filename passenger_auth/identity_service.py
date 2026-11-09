"""
Aadhaar / VID login and PNR binding (Phase 2, Feature A) on top of the mock UIDAI.

Flow:
  1. consent + Aadhaar number or VID → OTP to the phone linked with that identity
     (generic response: never reveals whether the number is registered);
  2. OTP verified → short-lived *identity session* (cookie `tnstc_idn`);
  3. PNR entered → accepted only if a passenger on that booking carries the SAME
     ID token (TNSTC records "Identity Type and ID No." per booking) AND the
     names agree; a normal passenger session is then issued for that passenger.

identity_otps document:
    subject (aadhaar token), phone, purpose (login | pickup), otpHash, otpSalt,
    createdAt, expiresAt, attempts, maxAttempts, status, cooldownUntil, purgeAt
It reuses the Feature 1 rules: atomic attempt reservation, single use, lockout.
"""
import hashlib
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from itsdangerous import BadSignature, URLSafeSerializer
from pymongo import ReturnDocument

from .bookings import BOOKINGS, find_eligible_booking, is_eligible
from .errors import AuthError
from .identity import AADHAAR, IdNumberError, find_citizen, id_token, mask_id, parse_id_number, token_key
from .otp import derive_key, generate_otp, hash_otp, is_valid_otp_format, new_salt, otp_matches
from .phone import mask_phone, try_normalize_phone
from .service import INVALID_OTP_MESSAGE, parse_pnr
from .timeutil import from_db, iso_utc, seconds_until, to_db, today_ist

logger = logging.getLogger(__name__)

IDENTITY_OTPS = "identity_otps"
IDENTITY_SESSIONS = "identity_sessions"
ID_COOKIE = "tnstc_idn"
LOGIN, PICKUP = "login", "pickup"

GENERIC_ID_MESSAGE = ("If this Aadhaar number or VID is registered, a 6-digit OTP has been sent "
                      "to the mobile number linked with it.")
NOT_LINKED_MESSAGE = ("This PNR is not linked to your Aadhaar. If you booked with another ID "
                      "(driving licence, voter ID, PAN…), log in with your PNR and mobile number instead.")
LINK_ATTEMPTS_PER_HOUR = 10


@dataclass(frozen=True)
class IdentitySession:
    session_id: str
    aadhaar_token: str
    last4: str
    kind: str
    name: str
    linked_phone: str
    expires_at: datetime

    @property
    def masked_id(self):
        return mask_id(self.last4)


def _name_tokens(name) -> list:
    text = unicodedata.normalize("NFC", str(name or "")).casefold()
    return [t for t in re.split(r"[^\w]+", text) if t and not t.isdigit()]


def names_match(a, b) -> bool:
    """
    Booking name vs UIDAI name. Accepts identical names, initials/abbreviations
    ("Arun K" ~ "Arun Kumar") and small spelling differences, so the same person
    is not turned away for how a booking clerk typed the name.
    """
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    short, long_ = sorted((ta, tb), key=len)
    if len(short) <= len(long_) and all(l.startswith(s) for s, l in zip(short, long_)):
        return True
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.85


class IdentityService:
    def __init__(self, db, config, sms, limiter, clock):
        self.db = db
        self.config = config
        self.sms = sms
        self.limiter = limiter
        self.clock = clock
        self.otps = db[IDENTITY_OTPS]
        self.sessions = db[IDENTITY_SESSIONS]
        self.token_key = token_key(config.signing_secret)
        self.hash_key = derive_key(config.signing_secret, "tnstc-id-otp-v1")
        self.serializer = URLSafeSerializer(config.signing_secret, salt="tnstc-identity-session")

    # ── parsing ──────────────────────────────────────────────────────────────
    def parse(self, raw) -> tuple:
        try:
            return parse_id_number(raw)
        except IdNumberError as e:
            raise AuthError(400, "invalid_request", str(e) + ".", fields={"idNumber": "invalid_format"})

    def subject_of(self, kind, digits) -> str:
        return id_token(self.token_key, kind, digits)

    # ── OTP core (shared by login and pickup) ────────────────────────────────
    def _active_lock(self, subject, purpose):
        return self.otps.find_one({"subject": subject, "purpose": purpose, "status": "locked",
                                   "cooldownUntil": {"$gt": to_db(self.clock())}},
                                  sort=[("cooldownUntil", -1)])

    def _locked_error(self, lock_doc):
        until = lock_doc["cooldownUntil"]
        return AuthError(423, "otp_locked", "Too many incorrect attempts. Verification is locked for now.",
                         retry_after=max(1, seconds_until(until, self.clock())), lockedUntil=iso_utc(until))

    def _issue(self, subject, phone, purpose, ip):
        now, cfg = self.clock(), self.config
        key = {"subject": subject, "purpose": purpose}
        self.otps.update_many({**key, "status": "active"}, {"$set": {"status": "superseded"}})
        otp, salt = generate_otp(), new_salt()
        self.otps.insert_one({
            **key, "phone": phone,
            "otpHash": hash_otp(self.hash_key, salt, f"{purpose}:{subject}", phone, otp), "otpSalt": salt,
            "createdAt": to_db(now), "expiresAt": to_db(now + timedelta(seconds=cfg.otp_ttl_seconds)),
            "attempts": 0, "maxAttempts": cfg.otp_max_attempts, "status": "active", "cooldownUntil": None,
            "ip": ip, "purgeAt": to_db(now + timedelta(seconds=cfg.record_retention_seconds)),
        })
        self.sms.send_otp(phone, otp, cfg.otp_ttl_seconds)

    def _verify(self, subject, phone, purpose, otp, reveal=True):
        """
        reveal=False (passenger login): every failure is the same generic 401, so
        the answers cannot tell a registered Aadhaar number from an unregistered one.
        """
        if not reveal:
            try:
                return self._verify(subject, phone, purpose, otp, reveal=True)
            except AuthError as e:
                if e.status in (401, 423):
                    raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE + " After several wrong "
                                    "attempts verification pauses for a while.")
                raise
        cfg = self.config
        if not is_valid_otp_format(otp):
            raise AuthError(400, "invalid_request", "The OTP must be exactly 6 digits.",
                            fields={"otp": "invalid_format"})
        lock = self._active_lock(subject, purpose)
        if lock:
            raise self._locked_error(lock)
        now = self.clock()
        current = self.otps.find_one({"subject": subject, "purpose": purpose, "status": "active"},
                                     sort=[("createdAt", -1)])
        if not current:
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)
        if from_db(current["expiresAt"]) <= now:
            self.otps.update_one({"_id": current["_id"], "status": "active"}, {"$set": {"status": "expired"}})
            raise AuthError(401, "otp_expired", "This OTP has expired. Please request a new one.")
        doc = self.otps.find_one_and_update(
            {"_id": current["_id"], "status": "active", "attempts": {"$lt": cfg.otp_max_attempts},
             "expiresAt": {"$gt": to_db(now)}},
            {"$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)
        if not otp_matches(self.hash_key, doc["otpSalt"], f"{purpose}:{subject}", phone, otp, doc["otpHash"]):
            if doc["attempts"] >= cfg.otp_max_attempts:
                until = now + timedelta(seconds=cfg.otp_cooldown_seconds)
                self.otps.update_one({"_id": doc["_id"], "status": "active"},
                                     {"$set": {"status": "locked", "cooldownUntil": to_db(until),
                                               "purgeAt": to_db(until + timedelta(
                                                   seconds=cfg.record_retention_seconds))}})
                raise self._locked_error({"cooldownUntil": to_db(until)})
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE,
                            attemptsRemaining=cfg.otp_max_attempts - doc["attempts"])
        won = self.otps.find_one_and_update({"_id": doc["_id"], "status": "active"},
                                            {"$set": {"status": "verified", "consumedAt": to_db(now)}})
        if won is None:
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE)

    def _ip_limit(self, scope, ip, limit):
        decision = self.limiter.hit_window(scope, ip, limit, self.config.otp_ip_window_seconds)
        if not decision.allowed:
            raise AuthError(429, "rate_limited", "Too many requests from your network. Please try again later.",
                            retry_after=decision.retry_after)

    # ── login: request / verify ──────────────────────────────────────────────
    def request_login_otp(self, raw_id, consent, ip) -> dict:
        if consent != "yes":
            raise AuthError(400, "consent_required",
                            "Please read and accept how your Aadhaar details are used before continuing.",
                            fields={"consent": "required"})
        kind, digits = self.parse(raw_id)
        cfg = self.config
        self._ip_limit("ip-id-otp-request", ip, cfg.otp_ip_request_limit)
        subject_probe = self.subject_of(kind, digits)
        count, retry = self.limiter.recent_events("id-otp-request", subject_probe, 3600)
        if count >= cfg.otp_max_requests_per_pnr_per_hour:
            raise AuthError(429, "id_request_limit", "Too many OTP requests for this number. Please try later.",
                            retry_after=retry)
        citizen = find_citizen(self.db, self.token_key, kind, digits)
        locked = bool(citizen and self._active_lock(citizen.aadhaar_token, LOGIN))
        gate = self.limiter.acquire_cooldown("id-otp-resend", subject_probe, cfg.otp_resend_seconds)
        if not gate.allowed:
            raise AuthError(429, "resend_cooldown", "Please wait before requesting another OTP.",
                            retry_after=gate.retry_after)
        self.limiter.record_event("id-otp-request", subject_probe, 3600)
        if citizen and not locked:            # locked: same generic answer, no SMS
            self._issue(citizen.aadhaar_token, citizen.linked_phone, LOGIN, ip)
            logger.info("Identity OTP issued for %s to %s.", mask_id(citizen.last4), mask_phone(citizen.linked_phone))
        else:
            logger.info("Identity OTP request for an unregistered number (generic response).")
        return {"expiresInSeconds": cfg.otp_ttl_seconds, "resendAfterSeconds": cfg.otp_resend_seconds}

    def verify_login_otp(self, raw_id, otp_raw, ip):
        """Returns the Citizen on success; raises AuthError otherwise."""
        kind, digits = self.parse(raw_id)
        otp = otp_raw.strip() if isinstance(otp_raw, str) else otp_raw
        self._ip_limit("ip-id-otp-verify", ip, self.config.otp_ip_verify_limit)
        citizen = find_citizen(self.db, self.token_key, kind, digits)
        if not citizen:
            if not is_valid_otp_format(otp):
                raise AuthError(400, "invalid_request", "The OTP must be exactly 6 digits.",
                                fields={"otp": "invalid_format"})
            raise AuthError(401, "otp_invalid", INVALID_OTP_MESSAGE + " After several wrong "
                            "attempts verification pauses for a while.")
        self._verify(citizen.aadhaar_token, citizen.linked_phone, LOGIN, otp, reveal=False)
        return citizen, kind

    # ── identity sessions ────────────────────────────────────────────────────
    def issue_session(self, citizen, kind, ip, user_agent):
        now = self.clock()
        expires = now + timedelta(seconds=self.config.session_ttl_seconds)
        sid = secrets.token_urlsafe(32)
        sid_hash = hashlib.sha256(sid.encode("ascii")).hexdigest()
        self.sessions.insert_one({
            "sidHash": sid_hash, "aadhaarToken": citizen.aadhaar_token, "last4": citizen.last4,
            "kind": kind, "name": citizen.name, "linkedPhone": citizen.linked_phone,
            "createdAt": to_db(now), "expiresAt": to_db(expires), "revokedAt": None, "ip": ip,
            "userAgent": (user_agent or "")[:200],
            "purgeAt": to_db(now + timedelta(seconds=self.config.record_retention_seconds)),
        })
        token = self.serializer.dumps({"sid": sid, "exp": int(expires.timestamp())})
        return token, IdentitySession(sid_hash, citizen.aadhaar_token, citizen.last4, kind, citizen.name,
                                      citizen.linked_phone, from_db(to_db(expires)))

    def load_session(self, token):
        """IdentitySession or None (missing / invalid / expired / revoked)."""
        if not token or len(token) > 1024:
            return None
        try:
            payload = self.serializer.loads(token)
        except BadSignature:
            return None
        if not (isinstance(payload, dict) and isinstance(payload.get("sid"), str)
                and isinstance(payload.get("exp"), int)) or payload["exp"] <= self.clock().timestamp():
            return None
        doc = self.sessions.find_one({"sidHash": hashlib.sha256(payload["sid"].encode("ascii")).hexdigest()})
        if not doc or doc.get("revokedAt") is not None or from_db(doc["expiresAt"]) <= self.clock():
            return None
        return IdentitySession(doc["sidHash"], doc["aadhaarToken"], doc["last4"], doc.get("kind", AADHAAR),
                               doc["name"], doc["linkedPhone"], from_db(doc["expiresAt"]))

    def revoke_token(self, token):
        session = self.load_session(token)
        if session:
            self.sessions.update_one({"sidHash": session.session_id, "revokedAt": None},
                                     {"$set": {"revokedAt": to_db(self.clock())}})

    # ── PNR binding (second factor: proof of journey) ────────────────────────
    def link_pnr(self, identity: IdentitySession, pnr_raw):
        """
        Returns (BookingMatch, passengerSeq) for the passenger on `pnr` whose
        booking ID token equals this identity's token and whose name agrees.
        """
        pnr = parse_pnr(pnr_raw)
        count, retry = self.limiter.recent_events("id-link", identity.aadhaar_token, 3600)
        if count >= LINK_ATTEMPTS_PER_HOUR:
            raise AuthError(429, "link_limit", "Too many PNR attempts. Please try again later.", retry_after=retry)
        self.limiter.record_event("id-link", identity.aadhaar_token, 3600)
        rows = [d for d in self.db[BOOKINGS].find({"pnr": pnr}).sort("passengerSeq", 1)
                if (d.get("idProof") or {}).get("token") == identity.aadhaar_token]
        if not rows or not names_match(rows[0].get("passengerName"), identity.name):
            raise AuthError(404, "pnr_not_linked", NOT_LINKED_MESSAGE)
        row = rows[0]
        today = today_ist(self.clock())
        if not is_eligible(row, today, self.config.booking_access_window_days):
            raise AuthError(403, "booking_not_eligible",
                            "This booking is cancelled or too old for Lost & Found access.")
        phone = try_normalize_phone(row.get("registeredPhone"))
        booking = find_eligible_booking(self.db, pnr, phone, today, self.config.booking_access_window_days)
        if booking is None:
            raise AuthError(404, "pnr_not_linked", NOT_LINKED_MESSAGE)
        logger.info("PNR %s linked to identity %s (passenger %s).", pnr, mask_id(identity.last4),
                    row.get("passengerSeq"))
        name = str(row.get("passengerName", "")).strip()
        return replace(booking, passenger_name=name, passenger_names=(name,)), int(row.get("passengerSeq") or 1)

    # ── pickup re-check (Feature F) ──────────────────────────────────────────
    def citizen_by_token(self, aadhaar_token):
        doc = self.db["mock_uidai"].find_one({"aadhaarToken": aadhaar_token, "status": "active"})
        return doc

    def send_pickup_otp(self, aadhaar_token, ip) -> str:
        """OTP to the Aadhaar-linked phone of a claim's owner. Returns the masked phone for staff."""
        doc = self.citizen_by_token(aadhaar_token)
        if not doc:
            raise AuthError(404, "identity_unavailable", "The identity record for this claim is unavailable.")
        lock = self._active_lock(aadhaar_token, PICKUP)
        if lock:
            raise self._locked_error(lock)
        gate = self.limiter.acquire_cooldown("pickup-otp-resend", aadhaar_token, self.config.otp_resend_seconds)
        if not gate.allowed:
            raise AuthError(429, "resend_cooldown", "Please wait before sending another OTP.",
                            retry_after=gate.retry_after)
        self._issue(aadhaar_token, doc["linkedPhone"], PICKUP, ip)
        return mask_phone(doc["linkedPhone"])

    def verify_pickup_otp(self, aadhaar_token, otp) -> None:
        doc = self.citizen_by_token(aadhaar_token)
        if not doc:
            raise AuthError(404, "identity_unavailable", "The identity record for this claim is unavailable.")
        self._verify(aadhaar_token, doc["linkedPhone"], PICKUP, otp.strip() if isinstance(otp, str) else otp)


def ensure_identity_indexes(db) -> None:
    specs = [
        (IDENTITY_OTPS, [("subject", 1), ("purpose", 1), ("status", 1), ("createdAt", -1)], {}),
        (IDENTITY_OTPS, [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        (IDENTITY_SESSIONS, [("sidHash", 1)], {"unique": True}),
        (IDENTITY_SESSIONS, [("purgeAt", 1)], {"expireAfterSeconds": 0}),
        ("mock_uidai", [("aadhaarToken", 1)], {"unique": True}),
        ("mock_uidai", [("vidToken", 1)], {"unique": True}),
    ]
    for name, keys, opts in specs:
        try:
            db[name].create_index(keys, **opts)
        except Exception as e:
            logger.warning("Index creation skipped for %s %s: %s", name, keys, e)
