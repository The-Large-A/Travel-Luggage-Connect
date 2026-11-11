"""
API tests for the two-layer passenger login (replaces the PNR + mobile tests).

Layer 1 — Aadhaar / VID + OTP against the encrypted mock UIDAI database.
Layer 2 — the ticket's PNR against the encrypted mock PNR database, bound to
          the same identity. No route issues a passenger session on one layer.
"""
import threading
from datetime import timedelta

import pytest

from conftest import (ARUN, CANCELLED, KARTHIK, MULTI_RAVI, MULTI_SHARED, OLD_JOURNEY, PRIYA, TEST_SECRET,
                      aadhaar_for, build_app, make_config, wrong_otp)
from passenger_auth.identity import mock_number_for, mock_vid_for, verhoeff_check_digit
from passenger_auth.sessions import COOKIE_NAME
from passenger_auth.stores import BookingStore
from passenger_auth.timeutil import from_db
from passenger_auth.vault import Vault

ARUN_NO = aadhaar_for(ARUN)


def _err(resp):
    return resp.get_json()["error"]


def _otp_count(sms):
    return sum(1 for m in sms.sent if m["purpose"] == "otp")


def _store(db):
    return BookingStore(db, Vault.from_env(TEST_SECRET))


def _unknown_number():
    body = "2222333344" + "5"
    return body + verhoeff_check_digit(body)


# ── Happy path: both layers ──────────────────────────────────────────────────
def test_end_to_end_two_layers(client, flow, sms):
    page = client.get("/passenger/login")
    assert page.status_code == 200 and b"Passenger Login" in page.data
    assert b"1 \xc2\xb7 Aadhaar / VID" in page.data and b"2 \xc2\xb7 Ticket PNR" in page.data
    assert b"PNR + mobile" not in page.data                          # no single-layer way in

    resp = flow.id_request(ARUN_NO)
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["expiresInSeconds"] == 300 and body["resendAfterSeconds"] == 30
    assert sms.sent[-1]["to"] == "+919876543210"

    otp = sms.last_otp()
    resp = flow.id_verify(ARUN_NO, otp)
    assert resp.status_code == 200 and otp not in resp.get_data(as_text=True)
    assert client.get("/auth/me").status_code == 401                 # layer 1 alone is NOT a login

    resp = flow.link(ARUN["pnr"])
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["passenger"] == {"pnr": "T26100101", "passengerName": "Arun Kumar",
                                 "maskedPhone": "+91 ******3210", "authMethod": "aadhaar",
                                 "maskedId": f"XXXX XXXX {ARUN_NO[-4:]}"}
    assert data["redirect"] == "/passenger"
    me = client.get("/auth/me")
    assert me.status_code == 200 and me.get_json()["booking"]["status"] == "confirmed"


def test_the_pnr_only_endpoints_are_gone(client):
    for path in ("/auth/otp/request", "/auth/otp/verify"):
        assert client.post(path, json={"pnr": ARUN["pnr"], "phone": ARUN["phone"]}).status_code == 404


def test_layer_two_without_layer_one_is_refused(client, flow):
    resp = flow.link(ARUN["pnr"])
    assert resp.status_code == 401 and _err(resp)["code"] == "identity_required"


def test_session_cookie_flags(flow):
    resp = flow.login(ARUN)
    cookie = next(h for h in resp.headers.getlist("Set-Cookie") if h.startswith(COOKIE_NAME + "="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie


def test_responses_are_not_cacheable(flow, client):
    assert flow.id_request(ARUN_NO).headers["Cache-Control"] == "no-store"
    assert client.get("/auth/id/me").headers["Cache-Control"] == "no-store"


# ── Layer 1 input validation and enumeration resistance ──────────────────────
@pytest.mark.parametrize("number", ["12345", "123456789012", "0" + ARUN_NO[1:], "abcd efgh ijkl",
                                    ARUN_NO[:-1] + str((int(ARUN_NO[-1]) + 1) % 10)])
def test_invalid_number_is_400_and_sends_nothing(flow, sms, number):
    resp = flow.id_request(number)
    assert resp.status_code == 400 and _err(resp)["fields"] == {"idNumber": "invalid_format"}
    assert not sms.sent


def test_unknown_number_gets_generic_response_and_no_sms(flow, sms):
    known, unknown = flow.id_request(ARUN_NO), flow.id_request(_unknown_number())
    assert unknown.status_code == known.status_code == 202
    assert unknown.get_json()["message"] == known.get_json()["message"]
    assert _otp_count(sms) == 1


@pytest.mark.parametrize("kwargs,status", [
    ({"data": "not json", "content_type": "text/plain"}, 400),
    ({"data": "{bad json", "content_type": "application/json"}, 400),
    ({"json": []}, 400),
    ({"json": {}}, 400),
    ({"json": {"idNumber": ARUN_NO}}, 400),                        # consent missing
    ({"json": {"idNumber": ARUN_NO, "consent": "yes", "extra": "x"}}, 400),
    ({"json": {"idNumber": "1" * 40, "consent": "yes"}}, 400),
    ({"data": "x" * 5000, "content_type": "application/json"}, 413),
])
def test_malformed_missing_oversized_payloads(client, sms, kwargs, status):
    assert client.post("/auth/id/otp/request", **kwargs).status_code == status
    assert not sms.sent


@pytest.mark.parametrize("payload", [
    {"idNumber": {"$ne": None}, "consent": "yes"},
    {"idNumber": [ARUN_NO], "consent": "yes"},
    {"idNumber": ARUN_NO, "consent": {"$gt": ""}},
    {"idNumber": 234567890124, "consent": "yes"},
])
def test_non_string_injection_is_rejected(client, sms, payload):
    resp = client.post("/auth/id/otp/request", json=payload)
    assert resp.status_code == 400 and not sms.sent


@pytest.mark.parametrize("pnr", ["T26100101'; DROP TABLE bookings;--", "T26100101\" || \"1\"==\"1",
                                 "$where", "T 26100101", "12345678", "TT26100101X"])
def test_layer_two_rejects_malformed_pnrs(flow, pnr):
    flow.verify_identity(ARUN_NO)
    resp = flow.link(pnr)
    assert resp.status_code == 400 and _err(resp)["fields"]["pnr"] in ("invalid_format", "too_long")


@pytest.mark.parametrize("otp", ["12345", "1234567", "12345a", "١٢٣٤٥٦", "🙂🙂🙂🙂🙂🙂", ""])
def test_malformed_otps_are_rejected(flow, otp):
    flow.id_request(ARUN_NO)
    assert flow.id_verify(ARUN_NO, otp).status_code == 400


def test_unicode_passenger_name_round_trips(flow, client):
    flow.login(KARTHIK)
    assert client.get("/auth/me").get_json()["passenger"]["passengerName"] == "கார்த்திக் செல்வம்"


# ── Layer 1 OTP lifecycle ─────────────────────────────────────────────────────
def test_expired_otp_is_a_generic_failure(flow, sms, clock):
    flow.id_request(ARUN_NO)
    otp = sms.last_otp()
    clock.advance(301)
    resp = flow.id_verify(ARUN_NO, otp)
    assert resp.status_code == 401 and _err(resp)["code"] == "otp_invalid"


def test_last_second_before_expiry_still_works(flow, sms, clock):
    flow.id_request(ARUN_NO)
    otp = sms.last_otp()
    clock.advance(299)
    assert flow.id_verify(ARUN_NO, otp).status_code == 200


def test_otp_cannot_be_reused(flow, sms):
    flow.id_request(ARUN_NO)
    otp = sms.last_otp()
    assert flow.id_verify(ARUN_NO, otp).status_code == 200
    assert flow.id_verify(ARUN_NO, otp).status_code == 401


def test_new_otp_supersedes_the_old_one(flow, sms, clock, db):
    flow.id_request(ARUN_NO)
    old = sms.last_otp()
    clock.advance(31)
    flow.id_request(ARUN_NO)
    new = sms.last_otp()
    if old == new:
        pytest.skip("CSPRNG produced the same code twice (p = 1e-6)")
    assert flow.id_verify(ARUN_NO, old).status_code == 401
    statuses = sorted(d["status"] for d in db.identity_otps.find({"purpose": "login"}))
    assert statuses == ["active", "superseded"]
    assert flow.id_verify(ARUN_NO, new).status_code == 200


def test_wrong_otp_locks_then_lock_expires(flow, sms, clock):
    flow.id_request(ARUN_NO)
    good = sms.last_otp()
    assert [flow.id_verify(ARUN_NO, wrong_otp(good)).status_code for _ in range(3)] == [401, 401, 401]
    assert flow.id_verify(ARUN_NO, good).status_code == 401          # locked, indistinguishable
    clock.advance(15 * 60 + 1)
    assert flow.id_request(ARUN_NO).status_code == 202
    assert flow.id_verify(ARUN_NO, sms.last_otp()).status_code == 200


def _parallel_verify(app, number, otps, monkeypatch):
    """Worst-case interleaving: every request reserves its attempt before any compares."""
    import mongomock
    real_fau = mongomock.collection.Collection.find_one_and_update
    before, after = threading.Barrier(len(otps)), threading.Barrier(len(otps))

    def fau(self, flt, update, *args, **kwargs):
        if "attempts" not in update.get("$inc", {}):
            return real_fau(self, flt, update, *args, **kwargs)
        before.wait(timeout=10)
        result = real_fau(self, flt, update, *args, **kwargs)
        after.wait(timeout=10)
        return result

    monkeypatch.setattr(mongomock.collection.Collection, "find_one_and_update", fau)
    results = [None] * len(otps)

    def worker(i, otp):
        results[i] = app.test_client().post("/auth/id/otp/verify",
                                            json={"idNumber": number, "otp": otp}).status_code

    threads = [threading.Thread(target=worker, args=(i, o)) for i, o in enumerate(otps)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_parallel_verify_only_one_succeeds(app, flow, sms, db, monkeypatch):
    flow.id_request(ARUN_NO)
    results = _parallel_verify(app, ARUN_NO, [sms.last_otp()] * 8, monkeypatch)
    assert results.count(200) == 1 and set(results) <= {200, 401}
    assert db.identity_sessions.count_documents({}) == 1


def test_parallel_wrong_guesses_never_exceed_max_attempts(app, flow, sms, db, monkeypatch):
    flow.id_request(ARUN_NO)
    good = sms.last_otp()
    guesses = [f"{(int(good) + k) % 1_000_000:06d}" for k in range(1, 13)]
    results = _parallel_verify(app, ARUN_NO, guesses, monkeypatch)
    assert 200 not in results
    doc = db.identity_otps.find_one({"purpose": "login"})
    assert doc["attempts"] == 3 and doc["status"] == "locked"


# ── Resend spam and rate limits ──────────────────────────────────────────────
def test_resend_cooldown(flow, sms, clock):
    assert flow.id_request(ARUN_NO).status_code == 202
    clock.advance(10)
    spam = flow.id_request(ARUN_NO)
    assert spam.status_code == 429 and _err(spam)["code"] == "resend_cooldown"
    assert spam.headers["Retry-After"] == "20" and _otp_count(sms) == 1
    clock.advance(20)
    assert flow.id_request(ARUN_NO).status_code == 202


def test_requests_capped_at_five_per_number_per_hour(flow, sms, clock):
    codes = []
    for _ in range(7):
        codes.append(flow.id_request(ARUN_NO).status_code)
        clock.advance(31)
    assert codes == [202] * 5 + [429, 429] and _otp_count(sms) == 5
    clock.advance(3600)
    assert flow.id_request(ARUN_NO).status_code == 202


def test_cap_applies_to_unregistered_numbers_too(flow, clock):
    codes = []
    for _ in range(6):
        codes.append(flow.id_request(_unknown_number()).status_code)
        clock.advance(31)
    assert codes == [202] * 5 + [429]


def test_per_ip_request_limit(db, clock, sms):
    client = build_app(db, clock, sms, make_config(otp_ip_request_limit=3)).test_client()
    numbers = [mock_number_for(n) for n in ("Arun Kumar", "Priya Raman", "Senthil Kumar", "Ravi Senthil")]
    codes = [client.post("/auth/id/otp/request", json={"idNumber": n, "consent": "yes"}).status_code
             for n in numbers]
    assert codes == [202, 202, 202, 429]
    other_ip = client.post("/auth/id/otp/request", json={"idNumber": numbers[3], "consent": "yes"},
                           environ_base={"REMOTE_ADDR": "10.0.0.2"})
    assert other_ip.status_code == 202


def test_per_ip_verify_limit(db, clock, sms):
    client = build_app(db, clock, sms, make_config(otp_ip_verify_limit=2)).test_client()
    codes = [client.post("/auth/id/otp/verify", json={"idNumber": ARUN_NO, "otp": "123456"}).status_code
             for _ in range(3)]
    assert codes == [401, 401, 429]


# ── Layer 2: the PNR must belong to the verified identity ────────────────────
def test_someone_elses_valid_pnr_is_refused(flow, client):
    flow.verify_identity(ARUN_NO)
    resp = flow.link(PRIYA["pnr"])
    assert resp.status_code == 404 and _err(resp)["code"] == "pnr_not_linked"
    assert client.get("/auth/me").status_code == 401


def test_unknown_pnr_is_refused(flow):
    flow.verify_identity(ARUN_NO)
    assert flow.link("T99999999").status_code == 404


def test_cancelled_and_old_bookings_fail_layer_two(flow):
    flow.verify_identity(aadhaar_for(CANCELLED))
    resp = flow.link(CANCELLED["pnr"])
    assert resp.status_code == 403 and _err(resp)["code"] == "booking_not_eligible"
    flow.verify_identity(aadhaar_for(OLD_JOURNEY))
    assert flow.link(OLD_JOURNEY["pnr"]).status_code == 403


def test_past_journey_inside_window_is_allowed(flow):
    flow.login(MULTI_RAVI)


def test_vid_passes_layer_one_for_the_same_person(flow, client):
    flow.verify_identity(mock_vid_for("Arun Kumar"))
    assert flow.link(ARUN["pnr"]).status_code == 200


def test_group_booking_binds_each_person_to_their_own_row(flow, client):
    flow.login(MULTI_SHARED)                                        # Senthil (shares a phone with Meena)
    assert client.get("/auth/me").get_json()["passenger"]["passengerName"] == "Senthil Kumar"


def test_registered_phone_stored_in_other_format_still_matches(db, flow):
    _store(db).update(PRIYA["pnr"], {"registeredPhone": "094440-12345"})
    flow.login(PRIYA)


# ── Sessions on protected routes ─────────────────────────────────────────────
def test_missing_session(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_missing"
    page = client.get("/passenger")
    assert page.status_code == 302 and "/passenger/login" in page.headers["Location"]


def test_expired_session(flow, client, clock):
    flow.login(ARUN)
    clock.advance(1799)
    assert client.get("/auth/me").status_code == 200
    clock.advance(1)
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_expired"


def test_expired_session_redirects_page_to_login(flow, client, clock):
    flow.login(ARUN)
    clock.advance(1800)
    page = client.get("/passenger")
    assert page.status_code == 302 and "reason=expired" in page.headers["Location"]
    assert b"Your session has expired" in client.get(page.headers["Location"]).data


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),
    lambda t: "x" + t,
    lambda t: "garbage",
    lambda t: "A" * 3000,
])
def test_tampered_session_rejected(flow, client, mutate):
    flow.login(ARUN)
    token = client.get_cookie(COOKIE_NAME).value
    client.set_cookie(COOKIE_NAME, mutate(token))
    resp = client.get("/auth/me")
    assert resp.status_code == 401 and _err(resp)["code"] == "session_invalid"


def test_forged_session_with_other_secret_rejected(flow, client):
    from itsdangerous import URLSafeSerializer
    flow.login(ARUN)
    forged = URLSafeSerializer("attacker-secret-0123456789-abcdefghij", salt="tnstc-passenger-session") \
        .dumps({"sid": "x", "pnr": "T26100102", "ph": "+919444012345", "exp": 9999999999})
    client.set_cookie(COOKIE_NAME, forged)
    assert client.get("/auth/me").status_code == 401


def test_session_record_bound_to_pnr_and_phone(flow, client, db):
    flow.login(ARUN)
    db.passenger_sessions.update_one({}, {"$set": {"pnr": "T26100102"}})
    assert client.get("/auth/me").status_code == 401


def test_logout_revokes_both_layers(flow, client):
    flow.login(ARUN)
    old_token = client.get_cookie(COOKIE_NAME).value
    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert client.get("/auth/id/me").status_code == 401
    client.set_cookie(COOKIE_NAME, old_token)                        # replay the captured cookie
    replay = client.get("/auth/me")
    assert replay.status_code == 401 and _err(replay)["code"] == "session_expired"


def test_logout_without_session_is_idempotent(client):
    assert client.post("/auth/logout").status_code == 200


def test_booking_cancelled_after_login_is_403(flow, client, db):
    flow.login(ARUN)
    _store(db).update(ARUN["pnr"], {"status": "cancelled"})
    resp = client.get("/auth/me")
    assert resp.status_code == 403 and _err(resp)["code"] == "booking_not_eligible"
    _store(db).update(ARUN["pnr"], {"status": "confirmed"})
    assert client.get("/auth/me").status_code == 401                  # that session was revoked


def test_session_ttl_configurable_and_server_side(db, clock, sms):
    from conftest import AuthFlow
    app = build_app(db, clock, sms, make_config(session_ttl_seconds=120))
    AuthFlow(app.test_client(), sms, clock).login(ARUN)
    doc = db.passenger_sessions.find_one()
    assert from_db(doc["expiresAt"]) - from_db(doc["createdAt"]) == timedelta(seconds=120)
    assert "sid" not in doc and len(doc["sidHash"]) == 64


# ── Redirects, CSRF, pages ───────────────────────────────────────────────────
@pytest.mark.parametrize("target,expected", [
    ("/status", "/status"), ("//evil.example", "/passenger"),
    ("https://evil.example", "/passenger"), ("/\\evil.example", "/passenger"),
])
def test_post_login_redirect_is_same_site_only(flow, target, expected):
    flow.verify_identity(ARUN_NO)
    assert flow.link(ARUN["pnr"], next_url=target).get_json()["redirect"] == expected


def test_csrf_enforced_on_both_layers(db, clock, sms):
    client = build_app(db, clock, sms, csrf=True).test_client()
    for path, body in (("/auth/id/otp/request", {"idNumber": ARUN_NO, "consent": "yes"}),
                       ("/auth/id/otp/verify", {"idNumber": ARUN_NO, "otp": "123456"}),
                       ("/auth/id/link", {"pnr": ARUN["pnr"]})):
        resp = client.post(path, json=body)
        assert resp.status_code == 403 and _err(resp)["code"] == "csrf_failed"
    assert not sms.sent


def test_csrf_token_flow_works(db, clock, sms):
    import re
    client = build_app(db, clock, sms, csrf=True).test_client()
    html = client.get("/passenger/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    resp = client.post("/auth/id/otp/request", json={"idNumber": ARUN_NO, "consent": "yes"},
                       headers={"X-CSRFToken": token})
    assert resp.status_code == 202


def test_login_page_redirects_when_already_logged_in(flow, client):
    flow.login(ARUN)
    resp = client.get("/passenger/login")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/passenger")


def test_login_page_resumes_at_layer_two_after_layer_one(flow, client):
    flow.verify_identity(ARUN_NO)
    html = client.get("/passenger/login").get_data(as_text=True)
    assert '"name": "Arun Kumar"' in html or '"name":"Arun Kumar"' in html


def test_nav_shows_passenger_when_logged_in(flow, client):
    assert b"Passenger Login" in client.get("/passenger/login").data
    flow.login(ARUN)
    assert b"Arun Kumar" in client.get("/passenger").data
