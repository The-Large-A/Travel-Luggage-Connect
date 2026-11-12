"""
The final-review features: strict Aadhaar↔PNR names and per-IP limits, the
encrypted loss-slip QR, the counter security check before a depot hands an item
over, the description clean-up gate and the shared strength meter.
"""
import json
import re

import pytest

import handover_check as hc
from conftest import ARUN, AuthFlow
from passenger_auth.identity import mock_number_for
from passenger_auth.identity_service import name_similarity, names_match
from test_match_e2e import _login, _log_found, _report_lost, _today, e2e, real_app  # noqa: F401,F811  (pytest fixtures)
from tracking_qr import MAX_AGE_SECONDS, QRError, TrackingQR


# ── Task 1: names and limits ─────────────────────────────────────────────────
@pytest.mark.parametrize("a,b,same", [
    ("Rajan", "Rajani", False), ("Arun Kumar", "  ARUN   KUMAR ", True), ("Mr. Arun Kumar", "Arun Kumar", True),
    ("Smt. Meena Senthil", "Meena Senthil", True), ("Kumar Arun", "Arun Kumar", True), ("Arun K", "Arun Kumar", True),
    ("Priya Raman", "Priya Ramanathan", False), ("Senthil Kumar", "Meena Senthil", False),
    ("Dr Lakshmi", "Lakshmi", True), ("", "Arun", False), ("கார்த்திக் செல்வம்", "கார்த்திக் செல்வம்", True),
])
def test_aadhaar_and_pnr_names_must_match_at_90_percent(a, b, same):
    assert names_match(a, b) is same
    assert name_similarity("Rajan", "Rajani") < 0.9


def test_a_pnr_whose_name_does_not_match_the_aadhaar_is_refused(client, sms, db):
    from passenger_auth.stores import BookingStore
    from passenger_auth.vault import Vault
    from conftest import TEST_SECRET
    BookingStore(db, Vault.from_env(TEST_SECRET)).update(ARUN["pnr"], {"passengerName": "Arunan Kumaresan"})
    flow = AuthFlow(client, sms, None)
    flow.verify_identity(mock_number_for("Arun Kumar"))
    resp = flow.link(ARUN["pnr"])
    assert resp.status_code == 404 and resp.get_json()["error"]["code"] == "pnr_not_linked"
    assert client.get("/auth/me").status_code == 401


def test_aadhaar_checks_are_limited_to_five_per_ip_per_hour(db, clock, sms):
    from conftest import build_app, make_config
    app = build_app(db, clock, sms, config=make_config(otp_ip_request_limit=5))
    client = app.test_client()
    numbers = [mock_number_for(n) for n in ("Arun Kumar", "Priya Raman", "Senthil Kumar", "Meena Senthil",
                                            "Ravi Senthil", "Kavitha Mani")]
    codes = [client.post("/auth/id/otp/request", json={"idNumber": n, "consent": "yes"}).status_code for n in numbers]
    assert codes[:5] == [202] * 5 and codes[5] == 429


def test_pnr_checks_are_limited_to_ten_per_ip_per_hour(db, clock, sms):
    from conftest import build_app, make_config
    app = build_app(db, clock, sms, config=make_config(pnr_ip_limit=10))
    client = app.test_client()
    flow = AuthFlow(client, sms, clock)
    flow.verify_identity(mock_number_for("Arun Kumar"))
    codes = [flow.link("T26199999").status_code for _ in range(11)]
    assert 429 in codes and codes.index(429) <= 10


# ── Task 2: the encrypted QR ─────────────────────────────────────────────────
def test_tracking_qr_is_encrypted_expires_and_detects_tampering():
    now = [1_800_000_000]
    qr = TrackingQR({"QR_FERNET_KEY": "", "SIGNING_SECRET": "s" * 40}, clock=lambda: now[0])
    token = qr.issue("TRK-ABCD1234", "req-42")
    assert "TRK" not in token and "req-42" not in token and "ABCD" not in token
    assert qr.read(token) == {"t": "TRK-ABCD1234", "r": "req-42", "ts": now[0]}
    with pytest.raises(QRError) as e:
        qr.read(token[:-6] + "AAAAAA")
    assert e.value.reason == "invalid"
    now[0] += MAX_AGE_SECONDS + 1
    with pytest.raises(QRError) as e:
        qr.read(token)
    assert e.value.reason == "expired"
    other = TrackingQR({"SIGNING_SECRET": "t" * 40})
    with pytest.raises(QRError):
        other.read(qr.issue("TRK-X", "r"))                    # another server's key cannot read it
    for bad in (None, "", "x" * 5000, "not-a-token"):
        with pytest.raises(QRError):
            qr.read(bad)


def test_a_report_gets_an_encrypted_qr_and_the_scan_endpoint_reads_it(e2e):
    app_module, db, sms = e2e
    client = _login(app_module.app.test_client(), sms, ARUN)
    _report_lost(client, "black Wildcraft backpack", _today())
    lost = db.lost_reports.find_one()
    assert lost["qrImagePath"].startswith("uploads/qr/") and lost["qrImagePath"].endswith(".svg")
    svg = open("static/" + lost["qrImagePath"], encoding="utf-8").read()
    assert svg.lstrip().startswith("<svg") and lost["tracking_id"] not in svg
    page = client.get("/").get_data(as_text=True)                 # the success card shows the QR once
    assert lost["qrImagePath"] in page
    token = app_module.tracking_qr.issue(lost["tracking_id"], lost["request_id"])
    anyone = app_module.app.test_client()
    ok = anyone.post("/api/qr/verify", json={"payload": f"https://x/verify/q/{token}"}).get_json()
    assert ok["valid"] and ok["trackingId"] == lost["tracking_id"] and ok["status"] == "pending"
    assert not {"passengerName", "name", "description", "phone"} & set(ok)      # status only
    assert anyone.post("/api/qr/verify", json={"payload": token[:-5] + "AAAAA"}).get_json()["reason"] == "invalid"
    assert anyone.get(f"/verify/q/{token}").status_code == 200
    assert anyone.get("/verify/q/garbage").status_code == 400


def test_qr_scans_are_rate_limited(e2e):
    app_module, db, sms = e2e
    anyone = app_module.app.test_client()
    codes = [anyone.post("/api/qr/verify", json={"payload": "x" * 20}).status_code for _ in range(32)]
    assert 429 in codes


# ── Task 3: the question engine ──────────────────────────────────────────────
def _found(**kw):
    doc = {"found_id": "f1", "category": "bag", "notes": "Black Wildcraft backpack with a steel tiffin box inside",
           "public_description": "Black backpack"}
    doc.update(kw)
    return doc


def test_answers_come_from_the_found_item_and_never_from_what_the_claimant_wrote():
    qs = hc.candidate_questions(_found(), {"description": "black backpack"}, {}, {})
    kinds = {q["attribute"]: q for q in qs}
    assert kinds["content_item"]["truth"] == "tiffin" and kinds["brand"]["truth"] == "Wildcraft"
    told = hc.candidate_questions(_found(), {"description": "Wildcraft bag with my tiffin"}, {}, {})
    assert not {"content_item", "brand"} & {q["attribute"] for q in told}        # the claimant said them
    assert "main_colour" not in {q["attribute"] for q in qs}                     # "Black backpack" is public


def test_colour_options_contrast_include_the_common_guess_and_avoid_the_items_other_colours():
    opts = hc._colour_options("red", ["yellow", "red"])
    names = [o.lower() for o in opts]
    assert names[0] == "red" and "black" in names and "yellow" not in names and len(set(names)) == 4
    # Red AND blue on the item: only gold, silver, white, yellow are left and no three of them are
    # distinguishable, so the question is not asked at all rather than with confusable options.
    assert hc._colour_options("red", ["blue", "red"]) is None
    from vocabulary import COLOUR_LAB, colours_confusable, delta_e2000
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not colours_confusable(a, b) and delta_e2000(COLOUR_LAB[a], COLOUR_LAB[b]) >= 25


def test_material_options_are_groups_not_synonyms():
    opts = hc._set_options("Leather", list(hc.MATERIALS), hc.MOST_COMMON["material"])
    assert opts[0] == "Leather" and "Fabric (nylon / cloth)" in opts and len(set(opts)) == 4
    assert not any("polyester" == o.lower() for o in opts)


def test_the_browser_never_gets_the_answer():
    q = [{"id": "q1", "attribute": "brand", "text": "Which brand?", "options": ["A", "B", "C", "D"],
          "answerIndex": 2, "source": "notes", "verified": True}]
    view = hc.public_view(q)
    assert view == [{"id": "q1", "text": "Which brand?", "options": ["A", "B", "C", "D"]}]


def test_staff_confirmation_replaces_the_ai_guess_and_skips_unsure_ones():
    qs = [{"id": "q1", "attribute": "style", "text": "?", "options": ["Backpack", "Duffel bag", "Handbag", "Trolley"],
           "answerIndex": 1, "verified": False, "source": "photo"},
          {"id": "q2", "attribute": "pattern", "text": "?", "options": ["Plain", "Striped", "Checked", "Printed"],
           "answerIndex": 0, "verified": False, "source": "photo"},
          {"id": "q3", "attribute": "brand", "text": "?", "options": ["Wildcraft", "VIP", "Safari", "Puma"],
           "answerIndex": 0, "verified": True, "source": "notes"}]
    final, (agreed, checked) = hc.apply_staff_check(qs, {"q1": 0, "q2": None})
    assert checked == 1 and agreed == 0                                # the AI said duffel, staff said backpack
    style = next(q for q in final if q["attribute"] == "style")
    assert style["options"][style["answerIndex"]] == "Backpack"
    assert {q["attribute"] for q in final} == {"style", "brand"}       # the skipped one is not asked


@pytest.mark.parametrize("correct,total,result", [(4, 4, "pass"), (3, 4, "pass"), (2, 4, "escalate"),
                                                  (1, 4, "fail"), (4, 5, "pass"), (3, 5, "escalate"),
                                                  (5, 6, "pass"), (4, 6, "escalate"), (2, 6, "fail")])
def test_pass_and_escalation_marks(correct, total, result):
    assert hc.outcome(correct, total) == result


def test_blind_guessing_rarely_passes():
    assert hc.guess_pass_probability(4) == pytest.approx(0.0508, abs=1e-3)
    assert hc.guess_pass_probability(6) < 0.005


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.chat = self
        self.completions = self

    def create(self, **kw):
        msg = type("M", (), {"content": self.reply})()
        return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


def test_llm_wording_is_used_only_when_it_passes_the_checks():
    q = [{"attribute": "material", "options": ["Leather", "Fabric (nylon / cloth)", "Plastic", "Metal"]}]
    good = _FakeLLM(json.dumps({"questions": [{"index": 0, "question": "What is the bag made of?",
                                                "distractors": ["Fabric (nylon / cloth)", "Denim", "Jute"]}]}))
    w = hc.llm_wording(q, "bag", good)
    assert w[0]["text"] == "What is the bag made of?"
    assert hc._valid_distractors({**q[0], "attribute": "material"}, w[0]["distractors"])
    leaky = _FakeLLM(json.dumps({"questions": [{"index": 0, "question": "Is the bag made of Leather?"}]}))
    assert not hc._valid_wording(hc.llm_wording(q, "bag", leaky)[0]["text"], q[0]["options"])
    assert hc.llm_wording(q, "bag", _FakeLLM("not json at all")) == {}
    synonym = {"attribute": "second_colour", "options": ["Blue", "Red", "Green", "White"]}
    assert hc._valid_distractors(synonym, ["Purple", "Black", "Red"]) is None        # purple ~ blue
    assert hc._valid_distractors({"attribute": "brand", "options": ["A", "B", "C", "D"]}, ["X", "Y", "Z"]) is None


# ── Task 3: the counter flow, end to end ─────────────────────────────────────
@pytest.fixture
def counter(e2e):
    app_module, db, sms = e2e
    flask_app = app_module.app
    passenger = _login(flask_app.test_client(), sms, ARUN)
    _report_lost(passenger, "black backpack, I lost it on the bus", _today())
    depot = flask_app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    resp = depot.post("/depot/found", data={
        "route_id": "R100", "trip_id": "T1", "date": _today(), "category": "bag",
        "notes": "Black Wildcraft backpack, steel tiffin box inside", "public_description": "Black backpack",
        "attr_zipper_tag_colour": "Red", "attr_distinguishing_mark": "CSK sticker"})
    assert resp.status_code == 302
    match = db.matches.find_one()
    return app_module, db, depot, match


def _url(match, what):
    return f"/api/matches/{match['_id']}/security-{what}"


def test_the_counter_check_end_to_end(counter):
    app_module, db, depot, match = counter
    assert match.get("securityQuestions") is not None                 # pre-generated with the match
    refused = depot.post("/api/match/resolve", json={"match_id": str(match["_id"])})
    assert refused.status_code == 409
    state = depot.get(_url(match, "questions")).get_json()
    assert state["ok"] and state["state"] in ("ready", "insufficient", "staff_check")
    # Staff hold the item: add a material question (the item is fabric) so there are enough.
    s = depot.post(_url(match, "staff-check"), json={"picks": {}, "staffName": "Counter 2",
                                                     "added": [{"attribute": "material", "value": "Fabric (nylon / cloth)"},
                                                               {"attribute": "pattern", "value": "Plain, one colour"}]})
    state = s.get_json()
    assert state["state"] == "ready" and len(state["questions"]) >= 4
    assert "answerIndex" not in json.dumps(state) and "private" not in json.dumps(state)
    stored = db.matches.find_one({"_id": match["_id"]})["securityQuestions"]
    assert {q["attribute"] for q in stored} >= {"private:zipper_tag_colour", "content_item", "brand", "material"}

    def answers(right):
        out = {}
        for q in db.matches.find_one({"_id": match["_id"]})["securityQuestions"]:
            if q.get("private"):
                good = next(i for i, o in enumerate(q["options"]) if o.lower() == "red")
            else:
                good = q["answerIndex"]
            out[q["id"]] = good if right else (good + 1) % len(q["options"])
        return out
    wrong = depot.post(_url(match, "answers"), json={"answers": answers(False)}).get_json()
    assert wrong["result"] == "fail" and wrong["attemptsLeft"] == 2
    assert "correct" not in json.dumps(wrong) and "score" not in json.dumps(wrong)   # outcome only
    right = depot.post(_url(match, "answers"), json={"answers": answers(True)}).get_json()
    assert right["result"] == "pass"
    log = db.matches.find_one({"_id": match["_id"]})["securityLog"]
    assert [e["event"] for e in log][-1] == "answered" and log[-1]["outcome"] == "pass"
    assert depot.post("/api/match/resolve", json={"match_id": str(match["_id"])}).status_code == 200


def test_three_failed_attempts_lock_the_check(counter):
    app_module, db, depot, match = counter
    depot.post(_url(match, "staff-check"), json={"picks": {}, "staffName": "C",
                                                 "added": [{"attribute": "material", "value": "Leather"},
                                                           {"attribute": "pattern", "value": "Striped"}]})
    for i in range(3):
        qs = db.matches.find_one({"_id": match["_id"]})["securityQuestions"]
        bad = {}
        for q in qs:
            if q.get("private"):
                bad[q["id"]] = next(j for j, o in enumerate(q["options"]) if o.lower() != "red")
            else:
                bad[q["id"]] = (q["answerIndex"] + 1) % len(q["options"])
        r = depot.post(_url(match, "answers"), json={"answers": bad}).get_json()
    assert r["result"] == "locked"
    assert depot.get(_url(match, "questions")).get_json()["state"] == "locked"
    assert depot.post(_url(match, "override"), json={"staffName": "X Y", "idChecked": True}).status_code == 423
    assert depot.post("/api/match/resolve", json={"match_id": str(match["_id"])}).status_code == 409


def test_staff_check_rules_and_access(counter):
    app_module, db, depot, match = counter
    anon = app_module.app.test_client()
    assert anon.get(_url(match, "questions")).status_code == 401
    other = app_module.app.test_client()
    other.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    db.matches.update_one({"_id": match["_id"]}, {"$set": {"depot_phone": "9999999999"}})
    assert other.get(_url(match, "questions")).status_code == 404               # another depot's match
    db.matches.update_one({"_id": match["_id"]}, {"$set": {"depot_phone": "9000000004"}})
    assert depot.post(_url(match, "answers"), json={"answers": "nope"}).status_code == 400
    assert depot.post(_url(match, "override"), json={"staffName": "A", "idChecked": True}).status_code in (400, 409)


def test_escalation_needs_a_staff_id_check_before_handover(counter):
    app_module, db, depot, match = counter
    depot.post(_url(match, "staff-check"), json={"picks": {}, "staffName": "C",
                                                 "added": [{"attribute": "material", "value": "Leather"}]})
    qs = db.matches.find_one({"_id": match["_id"]})["securityQuestions"]
    assert len(qs) == 4
    half = {}
    for n, q in enumerate(qs):                                  # exactly two right
        good = next(i for i, o in enumerate(q["options"]) if o.lower() == "red") if q.get("private") else q["answerIndex"]
        half[q["id"]] = good if n < 2 else (good + 1) % len(q["options"])
    r = depot.post(_url(match, "answers"), json={"answers": half}).get_json()
    assert r["result"] == "escalate"
    assert depot.post("/api/match/resolve", json={"match_id": str(match["_id"])}).status_code == 409
    assert depot.post(_url(match, "override"), json={"staffName": "R. Selvi", "idChecked": False}).status_code == 400
    assert depot.post(_url(match, "override"), json={"staffName": "R. Selvi", "idChecked": True,
                                                     "idLast4": "0124"}).get_json()["state"] == "passed"
    assert depot.post("/api/match/resolve", json={"match_id": str(match["_id"])}).status_code == 200
    log = db.matches.find_one({"_id": match["_id"]})["securityLog"]
    assert any(e["event"] == "staff-override" and e["by"] == "R. Selvi" for e in log)


def test_security_endpoints_are_rate_limited(counter):
    app_module, db, depot, match = counter
    codes = [depot.get(_url(match, "questions")).status_code for _ in range(62)]
    assert 429 in codes


# ── Task 5: description clean-up and strength ─────────────────────────────────
def test_cleanup_may_not_add_facts():
    from description_enhancer import enhance, safe
    assert safe("blk bp w/ tiffin", "Black backpack with a tiffin box.")
    assert not safe("a bag", "A black bag.")                          # invented colour
    assert not safe("blue bag", "Blue Wildcraft bag.")                 # invented brand
    assert not safe("bag with a phone", "Bag with phone, IMEI 123456789")
    reply = _FakeLLM("Black backpack containing a Dell laptop.")
    assert enhance("blk bp w/ dell laptop", client=reply) == "Black backpack containing a Dell laptop."
    assert enhance("a bag", client=_FakeLLM("A black Wildcraft bag.")) is None


def test_cleanup_endpoint_needs_a_session(e2e):
    app_module, db, sms = e2e
    anon = app_module.app.test_client()
    assert anon.post("/api/description/enhance", json={"text": "black bag"}).status_code == 401
    pax = _login(app_module.app.test_client(), sms, ARUN)
    body = pax.post("/api/description/enhance", json={"text": "black bag"}).get_json()
    assert body["ok"] and body["enhanced"] is None                    # no key in tests: original is used


@pytest.mark.parametrize("text,category,band", [
    ("bag", "bag", "weak"), ("black bag", "bag", "moderate"), ("black Wildcraft bag with a sticker", "bag", "strong"),
    ("Navy Wildcraft bag, tiffin inside, CSK sticker", "bag", "excellent"),
    ("Samsung phone, blue case, cracked", "phone", "excellent"),            # phones have no "contents"
])
def test_description_strength_bands(text, category, band):
    from vocabulary import description_strength
    assert description_strength(text, category)["band"] == band


def test_the_meter_is_on_both_forms(e2e):
    app_module, db, sms = e2e
    home = app_module.app.test_client().get("/").get_data(as_text=True)
    assert "description_meter.js" in home and "data-description-meter" in home
    depot = app_module.app.test_client()
    depot.post("/depot/login", data={"phone": "9000000004", "password": "pass123"})
    page = depot.get("/depot").get_data(as_text=True)
    assert re.search(r'<textarea name="notes"[^>]*data-description-meter', page)
    assert 'id="sec-modal"' in page and 'aria-modal="true"' in page and "handover_modal.js" in page


def test_stored_values_satisfy_the_mongodb_validators():
    """mongomock ignores $jsonSchema, so check the real constraints here (a 40 once crashed /lost)."""
    import re as _re
    from report_schema import FOUND_REPORTS_VALIDATOR, LOST_REPORTS_VALIDATOR
    from vocabulary import CATEGORY_LABELS, description_strength
    props = LOST_REPORTS_VALIDATOR["$jsonSchema"]["properties"]
    rule = props["detailScore"]
    texts = ["", "bag", "black bag", "black Wildcraft bag", "Samsung phone, blue case", "folder with my name",
             "black umbrella with a hook handle", "Navy Wildcraft bag, tiffin inside, CSK sticker"]
    for cat in CATEGORY_LABELS:
        for t in texts:
            score = description_strength(t, cat)["score"]
            assert isinstance(score, int) and rule["minimum"] <= score <= rule["maximum"], (cat, t, score)
    assert _re.fullmatch(props["qrImagePath"]["pattern"], "uploads/qr/abc123-def.svg")
    assert "notesEnhanced" in FOUND_REPORTS_VALIDATOR["$jsonSchema"]["properties"]


def test_llm_wording_must_name_what_it_asks_about():
    assert not hc._valid_wording("What type of object was found?", ["Book", "Clothes", "Towel", "Umbrella"],
                                 "content_item")                               # a real, ambiguous LLM output
    assert hc._valid_wording("Which of these was packed in your bag?", ["Book", "Clothes", "Towel", "Umbrella"],
                             "content_item")
    assert not hc._valid_wording("What is it?", ["Leather", "Plastic", "Metal", "Denim"], "material")


def test_a_staff_added_colour_question_never_offers_a_colour_the_item_has(counter):
    """Found live: 'which colour besides the main one?' once offered the bag's own main colour."""
    app_module, db, depot, match = counter
    db.matches.update_one({"_id": match["_id"]}, {"$set": {"securityItemColours": ["red", "black"]}})
    depot.post(_url(match, "staff-check"), json={"picks": {}, "staffName": "C",
                                                 "added": [{"attribute": "second_colour", "value": "Yellow"},
                                                           {"attribute": "main_colour", "value": "Red"}]})
    q = next(q for q in db.matches.find_one({"_id": match["_id"]})["securityQuestions"]
             if q["attribute"] == "second_colour")
    names = [o.lower() for o in q["options"]]
    assert "yellow" in names and not {"red", "black"} & set(names)
    assert "red" in q["text"].lower()                                  # "Apart from red, which other colour…"


def test_staff_can_replace_a_wrong_camera_colour(counter):
    """Candid photos: the camera read a black wallet as brown. Staff enter Black; Brown is never asked."""
    app_module, db, depot, match = counter
    wrong = {"id": "q9", "attribute": "main_colour", "text": "What is the main colour of your bag?",
             "options": ["Brown", "Green", "White", "Purple"], "answerIndex": 0, "verified": False, "source": "photo"}
    db.matches.update_one({"_id": match["_id"]}, {"$push": {"securityQuestions": wrong},
                                                  "$set": {"securityStaffChecked": False}})
    state = depot.get(_url(match, "questions")).get_json()
    assert state["state"] == "staff_check"
    assert "main_colour" in {a["attribute"] for a in state["addable"]}       # still addable despite the guess
    depot.post(_url(match, "staff-check"), json={"picks": {"q9": 0}, "staffName": "C",
                                                 "added": [{"attribute": "main_colour", "value": "Black"}]})
    qs = [q for q in db.matches.find_one({"_id": match["_id"]})["securityQuestions"] if q["attribute"] == "main_colour"]
    assert len(qs) == 1 and qs[0]["source"] == "staff"
    assert qs[0]["options"][qs[0]["answerIndex"]] == "Black" and "Brown" not in qs[0]["options"]
