"""
Phase 2, Feature A: mock Aadhaar / VID login, then the PNR as proof of journey.
"""
import pytest

from conftest import ARUN, PRIYA, TEST_SECRET
from passenger_auth.identity import (AADHAAR, MOCK_CITIZENS, MOCK_UIDAI, VID, IdNumberError, id_token,
                                     mock_number_for, mock_vid_for, parse_id_number, token_key,
                                     verhoeff_check_digit, verhoeff_valid)
from passenger_auth.identity_service import names_match

ARUN_AADHAAR = mock_number_for("Arun Kumar")
ARUN_VID = mock_vid_for("Arun Kumar")
PRIYA_AADHAAR = mock_number_for("Priya Raman")
MEENA_AADHAAR = mock_number_for("Meena Senthil")
RAVI_AADHAAR = mock_number_for("Ravi Senthil")
LAKSHMI_AADHAAR = mock_number_for("Lakshmi Narayanan")


# ── format rules ─────────────────────────────────────────────────────────────
def test_verhoeff_known_vector_and_check_digit():
    assert verhoeff_valid("2363")                      # textbook example: 236 → check digit 3
    assert verhoeff_check_digit("236") == "3"
    assert not verhoeff_valid("2364")


def test_every_mock_number_is_well_formed():
    for aadhaar, vid, *_ in MOCK_CITIZENS:
        assert parse_id_number(aadhaar) == (AADHAAR, aadhaar)
        assert parse_id_number(vid) == (VID, vid)


@pytest.mark.parametrize("raw", ["", "12345", "123456789012", "0" + ARUN_AADHAAR[1:],
                                 ARUN_AADHAAR[:-1] + str((int(ARUN_AADHAAR[-1]) + 1) % 10),
                                 "abcd efgh ijkl", "２３４５６７８９０１２４"])
def test_bad_numbers_are_rejected_before_any_sms(raw):
    with pytest.raises(IdNumberError):
        parse_id_number(raw)


def test_spaces_and_dashes_are_accepted():
    spaced = " ".join(ARUN_AADHAAR[i:i + 4] for i in range(0, 12, 4))
    assert parse_id_number(spaced) == (AADHAAR, ARUN_AADHAAR)
    assert parse_id_number(ARUN_AADHAAR[:4] + "-" + ARUN_AADHAAR[4:]) == (AADHAAR, ARUN_AADHAAR)


def test_the_mock_uidai_never_stores_a_number(db):
    for doc in db[MOCK_UIDAI].find():
        flat = repr(doc)
        for aadhaar, vid, *_ in MOCK_CITIZENS:
            assert aadhaar not in flat and vid not in flat
        assert len(doc["last4"]) == 4


def test_bookings_carry_only_a_token_and_last4(db):
    arun = db.bookings.find_one({"pnr": ARUN["pnr"]})
    assert arun["idProof"] == {"type": "aadhaar", "last4": ARUN_AADHAAR[-4:],
                               "token": id_token(token_key(TEST_SECRET), AADHAAR, ARUN_AADHAAR)}
    assert ARUN_AADHAAR not in repr(arun)
    assert db.bookings.find_one({"pnr": PRIYA["pnr"]})["idProof"]["type"] == "driving_licence"


@pytest.mark.parametrize("a,b,ok", [
    ("Arun Kumar", "Arun Kumar", True), ("ARUN KUMAR", "arun kumar", True), ("Arun K", "Arun Kumar", True),
    ("Arun Kumaar", "Arun Kumar", True), ("Priya Raman", "Arun Kumar", False), ("", "Arun Kumar", False),
    ("கார்த்திக் செல்வம்", "கார்த்திக் செல்வம்", True),
])
def test_name_matching(a, b, ok):
    assert names_match(a, b) is ok


# ── HTTP flow helpers ────────────────────────────────────────────────────────
def request_id_otp(client, number, consent="yes"):
    return client.post("/auth/id/otp/request", json={"idNumber": number, "consent": consent})


def verify_id(client, sms, number):
    assert request_id_otp(client, number).status_code == 202
    resp = client.post("/auth/id/otp/verify", json={"idNumber": number, "otp": sms.last_otp()})
    assert resp.status_code == 200, resp.get_json()
    return resp


def link(client, pnr):
    return client.post("/auth/id/link", json={"pnr": pnr})


# ── login ────────────────────────────────────────────────────────────────────
def test_aadhaar_login_then_pnr_link_issues_a_bound_session(client, sms):
    page = client.get("/passenger/login")
    assert b"Aadhaar / VID" in page.data and b"PNR + mobile" in page.data

    resp = request_id_otp(client, ARUN_AADHAAR)
    assert resp.status_code == 202
    assert "maskedPhone" not in resp.get_json()                 # existence is never revealed
    assert sms.sent[-1]["to"] == "+919876543210"                # the Aadhaar-linked phone

    resp = client.post("/auth/id/otp/verify", json={"idNumber": ARUN_AADHAAR, "otp": sms.last_otp()})
    assert resp.status_code == 200
    ident = resp.get_json()["identity"]
    assert ident["name"] == "Arun Kumar" and ident["maskedId"] == f"XXXX XXXX {ARUN_AADHAAR[-4:]}"
    assert ARUN_AADHAAR not in resp.get_data(as_text=True)

    resp = link(client, ARUN["pnr"])
    assert resp.status_code == 200, resp.get_json()
    pax = resp.get_json()["passenger"]
    assert pax["authMethod"] == "aadhaar" and pax["maskedId"].endswith(ARUN_AADHAAR[-4:])
    me = client.get("/auth/me").get_json()
    assert me["passenger"]["pnr"] == ARUN["pnr"] and me["passenger"]["authMethod"] == "aadhaar"


def test_vid_logs_in_the_same_person(client, sms):
    verify_id(client, sms, ARUN_VID)
    assert link(client, ARUN["pnr"]).status_code == 200


def test_consent_is_required(client, sms):
    resp = request_id_otp(client, ARUN_AADHAAR, consent="no")
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "consent_required"
    assert sms.sent == []


def test_unregistered_number_gets_the_same_generic_answer(client, sms):
    unknown = "2222333344" + "4"
    unknown = unknown[:11] + verhoeff_check_digit(unknown[:11])
    resp = request_id_otp(client, unknown)
    assert resp.status_code == 202 and sms.sent == []
    bad = client.post("/auth/id/otp/verify", json={"idNumber": unknown, "otp": "123456"})
    assert bad.status_code == 401 and bad.get_json()["error"]["code"] == "otp_invalid"


def test_wrong_otp_locks_after_the_limit(client, sms):
    assert request_id_otp(client, ARUN_AADHAAR).status_code == 202
    good = sms.last_otp()
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    codes = [client.post("/auth/id/otp/verify", json={"idNumber": ARUN_AADHAAR, "otp": wrong}).status_code
             for _ in range(3)]
    assert codes == [401, 401, 423]
    late = client.post("/auth/id/otp/verify", json={"idNumber": ARUN_AADHAAR, "otp": good})
    assert late.status_code == 423


def test_link_requires_a_verified_identity(client):
    resp = link(client, ARUN["pnr"])
    assert resp.status_code == 401 and resp.get_json()["error"]["code"] == "identity_required"


def test_someone_elses_pnr_is_refused(client, sms):
    verify_id(client, sms, ARUN_AADHAAR)
    resp = link(client, "T26100103")                            # Karthik's ticket
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "pnr_not_linked"
    assert client.get("/auth/me").status_code == 401


def test_booking_with_another_id_points_to_the_pnr_login(client, sms):
    verify_id(client, sms, PRIYA_AADHAAR)                      # Priya booked with a driving licence
    resp = link(client, PRIYA["pnr"])
    assert resp.status_code == 404
    assert "PNR and mobile number" in resp.get_json()["error"]["message"]


def test_cancelled_booking_is_not_eligible(client, sms):
    verify_id(client, sms, LAKSHMI_AADHAAR)
    resp = link(client, "T26100104")
    assert resp.status_code == 403 and resp.get_json()["error"]["code"] == "booking_not_eligible"


def test_group_booking_binds_the_session_to_the_verified_passenger(client, sms, app):
    verify_id(client, sms, MEENA_AADHAAR)                      # Meena shares Senthil's phone
    resp = link(client, "T26100105")
    assert resp.status_code == 200
    assert resp.get_json()["passenger"]["passengerName"] == "Meena Senthil"
    doc = app.extensions["passenger_auth"].db.passenger_sessions.find_one({"authMethod": "aadhaar"})
    assert doc["passengerSeqs"] == [2] and doc["idLast4"] == MEENA_AADHAAR[-4:]


def test_passenger_with_own_phone_on_a_group_booking(client, sms):
    verify_id(client, sms, RAVI_AADHAAR)
    assert link(client, "T26100105").get_json()["passenger"]["passengerName"] == "Ravi Senthil"


def test_link_attempts_are_rate_limited(client, sms):
    verify_id(client, sms, ARUN_AADHAAR)
    codes = [link(client, "T26100103").status_code for _ in range(11)]
    assert codes[:10] == [404] * 10 and codes[10] == 429


def test_logout_revokes_identity_too(client, sms):
    verify_id(client, sms, ARUN_AADHAAR)
    assert link(client, ARUN["pnr"]).status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/id/me").status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_pnr_login_still_works_as_the_fallback(flow, client):
    flow.login(ARUN)
    assert client.get("/auth/me").get_json()["passenger"]["authMethod"] == "pnr"
