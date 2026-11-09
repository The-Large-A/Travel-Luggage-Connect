"""
Phase 2, Feature F: identity re-check at pickup.

A claimant who logged in with (mock) Aadhaar must give the last 4 digits and
an OTP sent to the Aadhaar-linked phone; staff never see the digits and the
custody log keeps only the ID type and last 4. Others keep the photo-ID tick.
"""
import pytest

from conftest import ARUN, TEST_SECRET
from passenger_auth.identity import AADHAAR, id_token, mock_number_for, token_key

ARUN_NUMBER = mock_number_for("Arun Kumar")


@pytest.fixture
def aadhaar_claim(f3):
    client, claim_id = f3.approved_claim(ARUN)
    claim = f3.db.claims.find_one({"claimId": claim_id})
    f3.db.lost_reports.update_one({"request_id": claim["reportId"]}, {"$set": {"ownerIdentity": {
        "type": "aadhaar", "token": id_token(token_key(TEST_SECRET), AADHAAR, ARUN_NUMBER),
        "last4": ARUN_NUMBER[-4:]}}})
    token = f3.token(client, claim_id)["code"]
    tag = f3.db.found_reports.find_one({"found_id": claim["itemId"]})["qrTagId"]
    return f3, claim_id, token, tag


def complete(f3, token, tag, **extra):
    return f3.staff_client().post("/pickup/complete", json={"token": token, "qrTagId": tag, **extra})


def test_verify_tells_staff_which_check_to_run(aadhaar_claim):
    f3, _claim, token, _tag = aadhaar_claim
    summary = f3.staff_client().post("/pickup/verify", json={"token": token}).get_json()["claim"]
    assert summary["identityCheck"] == "aadhaar_otp"
    assert ARUN_NUMBER[-4:] not in repr(summary)                     # staff ask; the server compares


def test_photo_id_tick_is_not_enough_for_an_aadhaar_claim(aadhaar_claim):
    f3, _claim, token, tag = aadhaar_claim
    resp = complete(f3, token, tag, idChecked=True)
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "id_last4_required"


def test_wrong_last4_blocks_the_handover(aadhaar_claim):
    f3, _claim, token, tag = aadhaar_claim
    wrong = f"{(int(ARUN_NUMBER[-4:]) + 1) % 10000:04d}"
    sent_before = len(f3.sms.sent)
    resp = complete(f3, token, tag, idLast4=wrong, idOtp="123456")
    assert resp.status_code == 409 and resp.get_json()["error"]["code"] == "id_mismatch"
    assert len(f3.sms.sent) == sent_before                           # no OTP spent on a wrong person
    assert f3.db.found_reports.find_one({"qrTagId": tag})["status"] == "claim_locked"   # not handed over


def test_last4_plus_otp_completes_and_logs_only_last4(aadhaar_claim):
    f3, claim_id, token, tag = aadhaar_claim
    staff = f3.staff_client()
    sent = staff.post("/pickup/identity/otp", json={"token": token})
    assert sent.status_code == 200 and sent.get_json()["maskedPhone"].endswith("3210")
    otp = f3.sms.last_otp("+919876543210")
    assert complete(f3, token, tag, idLast4=ARUN_NUMBER[-4:], idOtp="000000" if otp != "000000" else "111111") \
        .status_code == 401                                                     # wrong OTP
    resp = complete(f3, token, tag, idLast4=ARUN_NUMBER[-4:], idOtp=otp)
    assert resp.status_code == 200, resp.get_json()
    entry = f3.db.custody_log.find_one({"claimId": claim_id, "event": "handover"})
    assert entry["idType"] == "aadhaar" and entry["idLast4"] == ARUN_NUMBER[-4:] and entry["otpVerified"]
    assert ARUN_NUMBER not in repr(entry)


def test_otp_is_required(aadhaar_claim):
    f3, _claim, token, tag = aadhaar_claim
    resp = complete(f3, token, tag, idLast4=ARUN_NUMBER[-4:])
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "id_otp_required"


def test_pnr_login_claims_keep_the_photo_id_check(f3):
    client, claim_id = f3.approved_claim(ARUN)
    token = f3.token(client, claim_id)["code"]
    claim = f3.db.claims.find_one({"claimId": claim_id})
    tag = f3.db.found_reports.find_one({"found_id": claim["itemId"]})["qrTagId"]
    staff = f3.staff_client()
    assert staff.post("/pickup/verify", json={"token": token}).get_json()["claim"]["identityCheck"] == "photo_id"
    assert staff.post("/pickup/identity/otp", json={"token": token}).status_code == 409
    assert complete(f3, token, tag, idChecked=True).status_code == 200
    entry = f3.db.custody_log.find_one({"claimId": claim_id, "event": "handover"})
    assert entry["idType"] == "photo_id" and entry["otpVerified"] is False
