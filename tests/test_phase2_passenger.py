"""
Phase 2 passenger features against the REAL app.py (ML models stubbed):
ticket autofill, Aadhaar-owned reports and the 7-day identity purge, report
details, luggage-path API, loss slip / verify page, status privacy, category-
aware scoring, and the translation fallback.
"""
from datetime import timedelta

import pytest

from conftest import ARUN, PRIYA
from test_match_e2e import ENV, _login, _today, e2e, real_app  # noqa: F401  (fixtures)
from passenger_auth.identity import mock_number_for, seed_mock_uidai
from passenger_auth.timeutil import utcnow
from seed_bookings import seed_bookings

SECRET = ENV["SIGNING_SECRET"]


def set_booking(app_module, pnr, fields, seq=None):
    """Bookings are encrypted at rest, so tests edit them through the store (never in clear)."""
    assert app_module.app.extensions["passenger_auth"].bookings.update(pnr, fields, seq=seq)


@pytest.fixture
def p2(e2e):
    app_module, db, sms = e2e
    identity = app_module.app.extensions["passenger_auth"].identity
    original_sms, identity.sms = identity.sms, sms
    seed_bookings(db, signing_secret=SECRET)
    seed_mock_uidai(db, SECRET)
    # Arun's ticket: route R100, trip T1, Chennai (S1) → Villupuram (S2), seat 12.
    set_booking(app_module, ARUN["pnr"], {
        "routeId": "R100", "tripId": "T1", "boardingStopId": "S1", "alightingStopId": "S2", "seatNo": 12,
        "travelDate": _today()})
    db.depots.update_one({"name": "Salem Depot"}, {"$set": {"publicPhone": "+91 44 2400 1234",
                                                             "whatsapp": "+91 94450 12345", "lat": 11.6, "lon": 78.1}})
    yield app_module, db, sms
    identity.sms = original_sms


def aadhaar_login(client, sms, name, pnr):
    number = mock_number_for(name)
    assert client.post("/auth/id/otp/request", json={"idNumber": number, "consent": "yes"}).status_code == 202
    assert client.post("/auth/id/otp/verify", json={"idNumber": number, "otp": sms.last_otp()}).status_code == 200
    resp = client.post("/auth/id/link", json={"pnr": pnr})
    assert resp.status_code == 200, resp.get_json()
    return client


def report(client, **overrides):
    data = {"route_id": "R100", "trip_id": "T1", "seat_no": "12", "boarding_stop_id": "S1",
            "alighting_stop_id": "S2", "date": _today(), "description": "Black Wildcraft backpack with a charger inside",
            "category": "bag", "storage_location": "overhead"}
    data.update(overrides)
    return client.post("/lost", data=data)


# ── ticket autofill ──────────────────────────────────────────────────────────
def test_journey_api_returns_the_ticket_journey(p2):
    app_module, db, sms = p2
    client = _login(app_module.app.test_client(), sms, ARUN)
    data = client.get("/api/passenger/journey").get_json()
    [pax] = data["passengers"]
    j = pax["journey"]
    assert j["route"]["route_id"] == "R100" and j["tripId"] == "T1" and j["seatNo"] == 12
    assert j["boardingStop"] == "Chennai CMBT" and j["alightingStop"] == "Villupuram"
    assert j["registrationNo"] == "TN 30 N 1234"


def test_unlinked_ticket_has_no_journey(p2):
    app_module, db, sms = p2
    client = _login(app_module.app.test_client(), sms, PRIYA)
    assert client.get("/api/passenger/journey").get_json()["passengers"][0]["journey"] is None


def test_ticket_fields_cannot_be_tampered_with(p2):
    app_module, db, sms = p2
    client = _login(app_module.app.test_client(), sms, ARUN)
    # A forged form: different seat and boarding stop, no route at all.
    report(client, route_id="", trip_id="", seat_no="33", boarding_stop_id="S2", alighting_stop_id="S3")
    lost = db.lost_reports.find_one({"ownerPnr": ARUN["pnr"]})
    assert lost["route_id"] == "R100" and lost["trip_id"] == "T1" and lost["seat_no"] == 12
    assert lost["boarding_stop_id"] == "S1"
    assert lost["alighting_stop_id"] == "S3" and lost["alightingChanged"] is True   # people get off later/earlier
    assert set(lost["autofilled"]) == {"route", "trip", "date", "boarding", "seat"}


def test_ticket_without_a_seat_lets_the_passenger_pick_one(p2):
    app_module, db, sms = p2
    set_booking(app_module, ARUN["pnr"], {"seatNo": None})
    client = _login(app_module.app.test_client(), sms, ARUN)
    report(client, seat_no="21")
    lost = db.lost_reports.find_one({})
    assert lost["seat_no"] == 21 and "seat" not in lost["autofilled"]


def test_group_booking_aadhaar_session_sees_only_its_own_reports(p2):
    app_module, db, sms = p2
    for seq, name in ((1, "Senthil Kumar"), (2, "Meena Senthil")):
        set_booking(app_module, "T26100105", {
            "routeId": "R100", "tripId": "T1", "boardingStopId": "S1", "alightingStopId": "S2",
            "seatNo": 10 + seq, "travelDate": _today()}, seq=seq)
    senthil = aadhaar_login(app_module.app.test_client(), sms, "Senthil Kumar", "T26100105")
    report(senthil, description="Senthil's brown leather wallet")
    tracking = db.lost_reports.find_one({"passengerSeq": 1})["tracking_id"]
    meena = aadhaar_login(app_module.app.test_client(), sms, "Meena Senthil", "T26100105")
    assert meena.get(f"/passenger/reports/{tracking}/slip").status_code == 404
    assert meena.get("/reports").get_json()["reports"] == []
    assert len(senthil.get("/reports").get_json()["reports"]) == 1


def test_alighting_before_boarding_is_refused_for_a_ticket(p2):
    app_module, db, sms = p2
    set_booking(app_module, ARUN["pnr"], {"boardingStopId": "S2", "alightingStopId": "S3"})
    client = _login(app_module.app.test_client(), sms, ARUN)
    report(client, alighting_stop_id="S1")
    assert db.lost_reports.count_documents({}) == 0


def test_report_details_are_stored(p2):
    app_module, db, sms = p2
    client = _login(app_module.app.test_client(), sms, ARUN)
    report(client, description="Navy Wildcraft bag, tiffin inside, CSK sticker", description_original="நீல பை",
           description_lang="ta", storage_location="boot", category="bag")
    lost = db.lost_reports.find_one({})
    assert lost["category"] == "bag" and lost["storageLocation"] == "boot"
    assert lost["descriptionOriginal"] == "நீல பை" and lost["descriptionLang"] == "ta"
    assert lost["detailScore"] == 100 and lost["ticketType"] == "pnr"
    ident = lost["ownerIdentity"]                           # two-layer login: token + last 4 only
    assert set(ident) == {"type", "token", "last4"} and ident["type"] == "aadhaar"
    assert mock_number_for("Arun Kumar") not in repr(lost) and lost["passengerSeq"] == 1


def test_unknown_category_and_storage_are_normalised(p2):
    app_module, db, sms = p2
    client = _login(app_module.app.test_client(), sms, ARUN)
    report(client, category="spaceship", storage_location="roof")
    lost = db.lost_reports.find_one({})
    assert lost["category"] == "other" and lost["storageLocation"] is None


# ── Aadhaar-owned reports and the 7-day purge ────────────────────────────────
def test_aadhaar_report_keeps_only_token_and_last4_then_purges(p2):
    app_module, db, sms = p2
    client = aadhaar_login(app_module.app.test_client(), sms, "Arun Kumar", ARUN["pnr"])
    report(client)
    lost = db.lost_reports.find_one({})
    number = mock_number_for("Arun Kumar")
    assert lost["ownerIdentity"]["last4"] == number[-4:] and lost["ownerIdentity"]["token"]
    assert number not in repr(lost)

    now = utcnow()
    assert app_module.purge_closed_identities(now) == 0                  # still open: kept
    db.lost_reports.update_one({}, {"$set": {"status": "resolved"}})
    assert app_module.purge_closed_identities(now) == 0                  # closed: purge date stamped
    stamped = db.lost_reports.find_one({})["identityPurgeAt"]
    assert stamped.date() == (now + timedelta(days=7)).date()
    assert app_module.purge_closed_identities(now + timedelta(days=7, minutes=1)) == 1
    lost = db.lost_reports.find_one({})
    assert "ownerIdentity" not in lost and lost["identityPurgedAt"]


# ── public surfaces ──────────────────────────────────────────────────────────
def test_status_api_no_longer_leaks_name_or_description(p2):
    app_module, db, sms = p2
    report(_login(app_module.app.test_client(), sms, ARUN))
    tracking = db.lost_reports.find_one({})["tracking_id"]
    body = app_module.app.test_client().get(f"/api/status/{tracking}").get_json()
    assert body["found"] and body["status"] == "pending"
    assert "name" not in body and "description" not in body


def test_loss_slip_for_the_owner_only(p2):
    app_module, db, sms = p2
    owner = _login(app_module.app.test_client(), sms, ARUN)
    report(owner)
    tracking = db.lost_reports.find_one({})["tracking_id"]
    page = owner.get(f"/passenger/reports/{tracking}/slip")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and tracking in html and "<svg" in html
    assert "Overhead rack" in html and "/verify/q/" in html
    assert f"/verify/{tracking}" not in html                              # the QR never carries the ID in clear
    other = _login(app_module.app.test_client(), sms, PRIYA)
    assert other.get(f"/passenger/reports/{tracking}/slip").status_code == 404


def test_verify_page_is_public_and_minimal(p2):
    app_module, db, sms = p2
    report(_login(app_module.app.test_client(), sms, ARUN), description="Secret laptop SN 99887766")
    tracking = db.lost_reports.find_one({})["tracking_id"]
    page = app_module.app.test_client().get(f"/verify/{tracking}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "Genuine report" in html
    assert "99887766" not in html and "Arun" not in html
    assert app_module.app.test_client().get("/verify/TRK-NOPE0000").status_code == 404


def test_downstream_api_shows_where_the_bus_went(p2):
    app_module, db, sms = p2
    data = app_module.app.test_client().get(
        "/api/transport/routes/R100/downstream?boarding=S1&alighting=S2").get_json()
    assert [s["stop_id"] for s in data["travelled"]] == ["S1", "S2"]
    assert [s["stop_id"] for s in data["downstream"]] == ["S2", "S3"]
    assert data["depots"][0]["name"] == "Salem Depot" and data["depots"][0]["publicPhone"]
    assert "password" not in repr(data) and "9000000004" not in repr(data)   # login phone never exposed
    assert data["homeDepot"]["name"] == "Salem Depot"
    bad = app_module.app.test_client().get("/api/transport/routes/R100/downstream?alighting=NOPE")
    assert bad.status_code == 400


def test_reverse_journey_downstream(p2):
    app_module, db, sms = p2
    data = app_module.app.test_client().get(
        "/api/transport/routes/R100/downstream?boarding=S3&alighting=S2").get_json()
    assert data["direction"] == "reverse"
    assert [s["stop_id"] for s in data["downstream"]] == ["S2", "S1"]


def test_vocabulary_api(p2):
    app_module, db, sms = p2
    v = app_module.app.test_client().get("/api/vocabulary").get_json()
    assert {c["key"] for c in v["categories"]} >= {"bag", "phone", "laptop"}
    assert "wildcraft" in v["brands"] and "சிவப்பு" in v["colours"]["red"]


def test_translate_without_a_key_keeps_the_original(p2):
    app_module, db, sms = p2
    resp = app_module.app.test_client().post("/api/translate", json={"text": "கருப்பு பை", "lang": "ta"})
    assert resp.status_code == 503 and resp.get_json()["code"] == "not_configured"


# ── category-aware scoring and multi-photo items ─────────────────────────────
def test_category_clash_is_a_contradiction_and_other_is_neutral(p2):
    app_module, db, sms = p2
    depot = db.depots.find_one({"name": "Salem Depot"})
    lost = {"route_id": "R100", "boarding_stop_id": "S1", "alighting_stop_id": "S2", "trip_id": "T1",
            "bus_id": "B1", "seat_no": 12, "description": "black dell laptop", "name": "Arun"}
    found = {"route_id": "R100", "trip_id": "T1", "bus_id": "B1", "notes": "black dell laptop", "ocr_text": ""}
    same = app_module.score_lost_found({**found, "category": "laptop"}, {**lost, "category": "laptop"}, depot)
    clash = app_module.score_lost_found({**found, "category": "umbrella"}, {**lost, "category": "laptop"}, depot)
    other = app_module.score_lost_found({**found, "category": "other"}, {**lost, "category": "laptop"}, depot)
    # +2 agreement → -6 contradiction, and Dell is no longer the item's own brand (Dell makes no umbrellas)
    assert same["bits"] - clash["bits"] >= 8.0
    assert clash["categoryMismatch"] and "category_clash" in clash["conflicts"]
    assert not other["categoryMismatch"] and other["bits"] >= clash["bits"] + 6     # "other": text says laptop
    assert same["final"] == other["final"] > clash["final"]     # "other" + notes saying laptop = laptop


def test_best_keyframe_wins_image_score(p2, monkeypatch):
    app_module, db, sms = p2
    scores = {"static/a.jpg": 0.2, "static/b.jpg": 0.9}
    monkeypatch.setattr(app_module.image_sim, "similarity",
                        lambda f, l: scores.get(f.replace("\\", "/"), 0.0))
    monkeypatch.setattr(app_module.image_sim, "available", True, raising=False)
    depot = db.depots.find_one({"name": "Salem Depot"})
    lost = {"route_id": "R100", "boarding_stop_id": "S1", "alighting_stop_id": "S2", "description": "x",
            "image_path": "uploads/l.jpg"}
    found = {"route_id": "R100", "notes": "y", "ocr_text": "", "images": ["a.jpg", "b.jpg"], "image_path": "a.jpg"}
    assert app_module.score_lost_found(found, lost, depot)["image"] == 0.9
