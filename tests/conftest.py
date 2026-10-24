"""
Shared fixtures for the passenger OTP authentication tests.

The suite never touches the real database or app.py (which loads ML models):
it builds a minimal Flask app with the passenger_auth blueprint on top of
mongomock, a controllable clock and an in-memory SMS provider that captures the
raw OTP the way a passenger's phone would receive it.
"""
import functools
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from passenger_auth import init_passenger_auth                       # noqa: E402
from passenger_auth.config import AuthConfig                          # noqa: E402
from passenger_auth.sms import SmsProvider                            # noqa: E402
from seed_bookings import seed_bookings                               # noqa: E402

TEST_SECRET = "test-signing-secret-0123456789-abcdefghijklmnopqrstuvwxyz"

# Seeded sample identities (see seed_bookings.SAMPLE_BOOKINGS).
ARUN = {"pnr": "T26100101", "phone": "9876543210"}
PRIYA = {"pnr": "T26100102", "phone": "9444012345"}
KARTHIK = {"pnr": "T26100103", "phone": "9840011223"}
CANCELLED = {"pnr": "T26100104", "phone": "9003312345"}
MULTI_SHARED = {"pnr": "T26100105", "phone": "9123456780"}
MULTI_RAVI = {"pnr": "T26100105", "phone": "8098765432"}
OLD_JOURNEY = {"pnr": "T26100106", "phone": "7010203040"}


class FakeClock:
    """Starts at the real current time (mongomock's TTL purge uses the wall clock)."""

    def __init__(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.now

    def advance(self, seconds):
        with self._lock:
            self.now += timedelta(seconds=seconds)


class RecordingSmsProvider(SmsProvider):
    """Captures every message, i.e. what the registered handset would receive."""
    name = "recording"

    def __init__(self):
        self.sent = []
        self._lock = threading.Lock()

    def _deliver(self, to_phone, body, redacted, purpose):
        with self._lock:
            self.sent.append({"to": to_phone, "body": body, "purpose": purpose})

    def last_otp(self, to_phone=None):
        for msg in reversed(self.sent):
            if msg["purpose"] == "otp" and (to_phone is None or msg["to"] == to_phone):
                return msg["body"].split(" ", 1)[0]
        return None


_ATOMIC_METHODS = ("find_one_and_update", "update_one", "update_many", "insert_one",
                   "find_one", "count_documents", "delete_many")


@pytest.fixture
def atomic_mongomock():
    """
    mongomock is not thread-safe, while real MongoDB guarantees that each single
    operation is atomic. Serializing each collection operation reproduces exactly
    that guarantee (and no more), so concurrency tests are meaningful.
    """
    lock = threading.RLock()
    originals = {name: getattr(mongomock.collection.Collection, name) for name in _ATOMIC_METHODS}

    def wrap(fn):
        @functools.wraps(fn)
        def locked(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)
        return locked

    for name, fn in originals.items():
        setattr(mongomock.collection.Collection, name, wrap(fn))
    yield
    for name, fn in originals.items():
        setattr(mongomock.collection.Collection, name, fn)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db(clock, atomic_mongomock):
    database = mongomock.MongoClient()["tn_bus_lost_found_test"]
    seed_bookings(database, now=clock())
    return database


@pytest.fixture
def sms():
    return RecordingSmsProvider()


def make_config(**overrides):
    base = dict(signing_secret=TEST_SECRET, otp_ip_request_limit=1000, otp_ip_verify_limit=1000)
    base.update(overrides)
    return AuthConfig(**base)


def build_app(db, clock, sms, config=None, csrf=False):
    templates = os.path.join(ROOT, "templates")
    app = Flask(__name__, template_folder=templates)
    app.config.update(SECRET_KEY="flask-test-key", TESTING=True, WTF_CSRF_ENABLED=csrf)
    CSRFProtect(app)

    # Endpoints that base.html links to; the real ones live in app.py.
    for rule, endpoint in (("/", "index"), ("/status", "status_page"),
                           ("/depot-login", "depot_login_page"), ("/depot/logout", "depot_logout")):
        app.add_url_rule(rule, endpoint, lambda: "stub")

    init_passenger_auth(app, db, config=config or make_config(), sms_provider=sms, clock=clock)
    return app


@pytest.fixture
def app(db, clock, sms):
    return build_app(db, clock, sms)


@pytest.fixture
def client(app):
    return app.test_client()


class AuthFlow:
    """Small helper so tests read as the passenger journey."""

    def __init__(self, client, sms, clock):
        self.client, self.sms, self.clock = client, sms, clock

    def request(self, pnr, phone, **kw):
        return self.client.post("/auth/otp/request", json={"pnr": pnr, "phone": phone}, **kw)

    def verify(self, pnr, phone, otp, **kw):
        return self.client.post("/auth/otp/verify", json={"pnr": pnr, "phone": phone, "otp": otp}, **kw)

    def login(self, ident):
        assert self.request(ident["pnr"], ident["phone"]).status_code == 202
        otp = self.sms.last_otp()
        resp = self.verify(ident["pnr"], ident["phone"], otp)
        assert resp.status_code == 200, resp.get_json()
        return resp


@pytest.fixture
def flow(client, sms, clock):
    return AuthFlow(client, sms, clock)


def wrong_otp(correct: str) -> str:
    return f"{(int(correct) + 1) % 1_000_000:06d}"
