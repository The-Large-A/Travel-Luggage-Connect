"""
Phase 2, Feature C/D: the depot "Add new lost item" camera scan.

OpenCV gating is tested on synthetic images; the whole flow (draft → frames →
close-ups → automatic save → found item + questions + matching) runs against
the REAL app.py with CLIP / OCR replaced by deterministic fakes.
"""
import os

import cv2
import numpy as np
import pytest

from test_match_e2e import _today, e2e, real_app  # noqa: F401  (fixtures)
from claims.attributes import encode_private_attributes, question_view, select_questions
from item_intake import vision
from item_intake.analysis import looks_like_id, redact_ids
from item_intake.captioner import BasicCaptioner
from item_intake.questions import build_answers, public_description
from vocabulary import BRANDS, CONTENT_POOLS

RNG = np.random.default_rng(3)


# ── synthetic views of a "bag" ───────────────────────────────────────────────
def view(angle=0, colour=(30, 30, 200), shift=0, size=(480, 640), text=None, blur=0, light=1.0):
    """A textured coloured block on a plain background, rotated per view (BGR)."""
    h, w = size
    img = np.full((h, w, 3), 228, np.uint8)
    box = cv2.boxPoints(((w // 2 + shift, h // 2), (300, 220), angle)).astype(np.int32)
    cv2.fillPoly(img, [box], colour)
    noise = RNG.integers(-25, 25, img.shape, dtype=np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    cv2.line(img, tuple(box[0]), tuple(box[2]), (20, 20, 20), 4)          # a strap / edge detail
    if text:
        cv2.putText(img, text, (90, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 0), 3)
    if blur:
        img = cv2.GaussianBlur(img, (blur, blur), 0)
    return np.clip(img.astype(np.float32) * light, 0, 255).astype(np.uint8)


def jpeg(img):
    return vision.encode_jpeg(img, 90)


# ── OpenCV unit tests ────────────────────────────────────────────────────────
def test_sharpness_separates_sharp_from_blurred():
    assert vision.sharpness(view()) > 200
    assert vision.sharpness(view(blur=41)) < 10


def test_exposure_detects_dark_frames():
    assert vision.exposure(view(light=0.08))["mean"] < 15
    assert 60 < vision.exposure(view())["mean"] < 95


def test_phash_tells_new_views_from_repeats():
    a, a2, b = view(0), view(0), view(55, shift=60)
    assert vision.hamming(vision.phash(a), vision.phash(a2)) < 10
    assert vision.hamming(vision.phash(a), vision.phash(b)) >= 10


def test_item_colour_comes_from_the_item_not_the_background():
    img = view(colour=(30, 30, 200))                     # red block (BGR) on light grey
    names = [c[0] for c in vision.dominant_colours(img, vision.item_mask(img))]
    assert names[0] == "red"


def test_text_regions_found_only_where_text_is():
    assert vision.text_regions(view(text="ARUN KUMAR"))
    assert not vision.text_regions(np.full((480, 640, 3), 200, np.uint8))


def test_blur_regions_destroys_the_text():
    img = view(text="2345 6789 0124")
    boxes = vision.text_regions(img)
    out = vision.blur_regions(img, boxes)
    x, y, w, h = boxes[0]
    assert vision.sharpness(out[y:y + h, x:x + w]) < vision.sharpness(img[y:y + h, x:x + w]) / 4


def test_id_numbers_are_detected_and_redacted():
    assert looks_like_id("aadhaar 2345 6789 0124") and looks_like_id("234567890124")
    assert not looks_like_id("wildcraft 44 litre")
    assert "6789" not in redact_ids("no 2345 6789 0124 here")


# ── answers / multiple choice ────────────────────────────────────────────────
FACTS = {"category": "bag", "categoryConfidence": 0.9, "noun": "bag",
         "colours": [{"name": "black", "share": 0.7}, {"name": "red", "share": 0.2}],
         "detailColour": {"name": "yellow", "confidence": 0.8},
         "contents": [{"name": "tiffin", "confidence": 0.82}],
         "brand": {"name": "Wildcraft", "confidence": 0.9},
         "tagText": {"text": "ARUN K", "confidence": 0.85},
         "features": [{"name": "keychain", "confidence": 0.5}]}


def test_only_confident_facts_become_questions():
    raw, choices, report, qcat = build_answers(FACTS, 0.6)
    assert qcat == "bag"
    assert set(raw) == {"zipper_tag_colour", "content_item", "brand", "tag_text"}
    assert report["distinguishing_mark"]["asQuestion"] is False           # 0.5 < 0.6
    assert "Wildcraft" not in repr(report) and "ARUN" not in repr(report)  # report never holds values
    zip_opts = [o.lower() for o in choices["zipper_tag_colour"]]
    assert zip_opts[0] == "yellow" and len(zip_opts) == 4
    # v1 offered yellow / orange / gold — the shades an owner mixes up. Now: none of them.
    assert not {"orange", "gold", "white"} & set(zip_opts)
    assert set(choices["brand"]) <= set(BRANDS["bag"])                    # bag brands only, never Acer/Redmi
    assert raw["content_item"] == "Tiffin" and "Tiffin" in choices["content_item"]
    assert set(o.lower() for o in choices["content_item"]) <= set(CONTENT_POOLS["bag"])   # things a bag holds
    assert public_description(FACTS) == "Black bag"


def test_questions_follow_the_item_kind():
    docs = {**FACTS, "category": "documents", "noun": "document folder"}
    raw, _choices, report, qcat = build_answers(docs, 0.6)
    assert qcat == "documents" and "brand" not in raw and "zipper_tag_colour" not in raw   # no brand of a file
    assert report["brand"]["skipped"] == "does not suit this item"
    assert {"content_item", "tag_text"} <= set(raw)


def test_unsure_category_gets_only_questions_that_suit_anything():
    unsure = {**FACTS, "categoryConfidence": 0.55, "categoryMargin": 0.1}
    raw, choices, report, qcat = build_answers(unsure, 0.6)
    assert qcat == "other" and set(raw) == {"tag_text"} and not choices
    assert report["brand"]["skipped"] == "category uncertain"
    close_call = {**FACTS, "categoryConfidence": 0.85, "categoryMargin": 0.12}           # sure, but close 2nd
    assert build_answers(close_call, 0.6)[3] == "other"


def test_choice_questions_render_options_and_still_hash_the_answer():
    raw, choices, _, _ = build_answers(FACTS, 0.6)
    key = b"k" * 32
    attrs = encode_private_attributes(raw, "bag", "Black bag", key, 5, choices=choices)
    assert "Wildcraft" not in repr({k: v["entries"] for k, v in attrs.items()})
    view_q = question_view("brand", attrs)
    assert sorted(view_q["options"]) == sorted(choices["brand"]) and len(view_q["options"]) == 4
    assert "options" not in question_view("tag_text", attrs)                # free text stays free text
    item = {"category": "bag", "private_attributes": attrs}
    for _ in range(20):
        chosen, _reserve = select_questions(item)
        kinds = ["choice" if attrs[q].get("choices") else "text" for q in chosen]
        assert "text" in kinds and kinds.count("choice") <= 2              # never recognition alone


def test_a_round_is_never_only_multiple_choice():
    raw = {"zipper_tag_colour": "yellow", "content_item": "tiffin", "brand": "Wildcraft"}
    choices = {"zipper_tag_colour": ["yellow", "orange", "gold"], "content_item": ["tiffin", "book", "keys"],
               "brand": ["Wildcraft", "Skybags", "VIP"]}
    attrs = encode_private_attributes(raw, "bag", "Black bag", b"k" * 32, 5, choices=choices)
    assert select_questions({"category": "bag", "private_attributes": attrs}) == ([], None)   # → staff review


def test_reserve_question_prefers_hidden_free_text():
    raw = {"zipper_tag_colour": "yellow", "content_item": "tiffin", "brand": "Wildcraft",
           "tag_text": "ARUN K", "distinguishing_mark": "CSK sticker"}
    choices = {"zipper_tag_colour": ["yellow", "orange", "gold"], "content_item": ["tiffin", "book", "keys"],
               "brand": ["Wildcraft", "Skybags", "VIP"]}
    attrs = encode_private_attributes(raw, "bag", "Black bag", b"k" * 32, 5, choices=choices)
    for _ in range(20):
        chosen, reserve = select_questions({"category": "bag", "private_attributes": attrs})
        texts = [q for q in chosen if "choices" not in attrs[q]]
        assert texts and sum(1 for q in chosen if "choices" in attrs[q]) <= 2
        assert {"content_item", "tag_text"} & set(chosen)                 # every round asks something hidden
        if reserve and "tag_text" not in chosen:
            assert reserve == "tag_text"                                  # hidden AND free text first


def test_invalid_choice_lists_are_dropped():
    attrs = encode_private_attributes({"brand": "Wildcraft"}, "bag", "", b"k" * 32, 5,
                                      choices={"brand": ["Skybags", "VIP"]})            # missing the truth
    assert "choices" not in attrs["brand"]


# ── the full flow against the real app ───────────────────────────────────────
class FakeClassifier:
    available = True

    def __init__(self):
        self.calls = 0

    def embed(self, path):
        return None

    def zero_shot(self, paths, labels, template="a photo of {}"):
        self.calls += 1
        scores = {label: 0.05 for label in labels}
        for label in labels:
            if label.startswith("a bag, backpack"):
                scores[label] = 3.0
            elif label == "a nylon bag":
                scores[label] = 2.0
            elif "with shoulder strap" in label or "with a steel tiffin box" in label:
                scores[label] = 4.0
            elif label in ("a bag", "an open bag"):
                scores[label] = 1.0
        total = sum(scores.values())
        return {k: v / total for k, v in scores.items()}


class FakeOCR:
    def __init__(self, text="wildcraft arun kumar"):
        self.text = text

    def extract_text(self, path):
        return self.text if ("tag" in path or "detail" in path) else ""


@pytest.fixture
def intake(e2e, tmp_path, monkeypatch):
    app_module, db, sms = e2e
    svc = app_module.app.extensions["item_intake"].service
    monkeypatch.setattr(svc, "classifier", FakeClassifier())
    monkeypatch.setattr(svc, "ocr", FakeOCR())
    monkeypatch.setattr(svc, "captioner", BasicCaptioner())
    monkeypatch.setattr(svc, "async_jobs", False)
    monkeypatch.setattr(svc, "upload_root", str(tmp_path))
    staff = app_module.app.test_client()
    with staff.session_transaction() as s:
        s["depot_phone"], s["depot_name"] = "9000000004", "Salem Depot"
    return app_module, db, svc, staff


def new_draft(staff):
    resp = staff.post("/api/intake/drafts")
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["draft"]["draftId"]


def send(staff, draft, img, step="outside"):
    from io import BytesIO
    return staff.post(f"/api/intake/drafts/{draft}/frames",
                      data={"step": step, "frame": (BytesIO(jpeg(img)), "f.jpg")},
                      content_type="multipart/form-data").get_json()


def closeup(staff, draft, img, kind="tag"):
    from io import BytesIO
    return staff.post(f"/api/intake/drafts/{draft}/closeups",
                      data={"kind": kind, "photo": (BytesIO(jpeg(img)), "c.jpg")},
                      content_type="multipart/form-data").get_json()


def capture_item(staff):
    draft = new_draft(staff)
    kept = [send(staff, draft, view(a, shift=s))["kept"] for a, s in ((0, 0), (40, 40), (80, -40), (120, 70))]
    assert kept == [True, True, True, True]
    assert send(staff, draft, view(90, colour=(40, 40, 40)), step="inside")["kept"]
    assert closeup(staff, draft, view(text="WILDCRAFT ARUN"), "tag")["kept"]
    assert closeup(staff, draft, view(colour=(30, 210, 235)), "detail")["kept"]     # yellow zip pull
    return draft


def test_frame_gate_rejects_bad_and_repeated_views(intake):
    app_module, db, svc, staff = intake
    draft = new_draft(staff)
    assert send(staff, draft, view())["kept"] is True
    again = send(staff, draft, view())
    assert again["kept"] is False and "new side" in again["hint"]
    assert "blurry" in send(staff, draft, view(30, blur=41))["hint"]
    assert "dark" in send(staff, draft, view(60, light=0.05))["hint"]
    assert send(staff, draft, view(70, shift=50))["views"] == 2


def test_not_an_image_is_refused(intake):
    from io import BytesIO
    app_module, db, svc, staff = intake
    draft = new_draft(staff)
    resp = staff.post(f"/api/intake/drafts/{draft}/frames",
                      data={"step": "outside", "frame": (BytesIO(b"not a jpeg"), "f.jpg")},
                      content_type="multipart/form-data")
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "not_an_image"


def test_scan_saves_the_item_automatically(intake):
    app_module, db, svc, staff = intake
    draft = capture_item(staff)
    resp = staff.post(f"/api/intake/drafts/{draft}/finalize",
                      json={"route_id": "R100", "trip_id": "T1", "date": _today(), "found_seat_area": "12"})
    assert resp.status_code == 202, resp.get_json()
    d = staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]
    assert d["status"] == "done" and d["progress"]["pct"] == 100
    item = db.found_reports.find_one({"found_id": d["result"]["foundId"]})
    assert item["captureMode"] == "camera_scan" and item["category"] == "bag"
    assert item["public_description"] == "Red bag" and len(item["images"]) == 7
    assert item["trip_id"] == "T1" and item["found_seat_area"] == 12 and item["qrTagId"].startswith("TAG-")
    assert "Wildcraft" in item["notes"] and "tiffin" in item["notes"]          # staff-only text for the matcher
    attrs = item["private_attributes"]
    assert {"brand", "content_item", "zipper_tag_colour", "tag_text"} <= set(attrs)
    assert attrs["brand"]["choices"] and "Wildcraft" in attrs["brand"]["choices"]
    assert "options" not in question_view("tag_text", attrs)
    assert "Wildcraft" not in repr(item["attributes"])                          # report: confidences only
    assert db.custody_log.find_one({"itemId": item["found_id"], "event": "logged"})
    assert set(d["result"]["questions"]) >= {"brand", "content_item"}


def test_scan_masks_id_numbers_before_saving(intake, monkeypatch):
    app_module, db, svc, staff = intake
    monkeypatch.setattr(svc, "ocr", FakeOCR("aadhaar 2345 6789 0124 arun"))
    draft = capture_item(staff)
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    d = staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]
    item = db.found_reports.find_one({"found_id": d["result"]["foundId"]})
    assert "6789" not in item["notes"] and "6789" not in item["ocr_text"]
    assert d["facts"]["maskedIds"] >= 1 and "masked" in item["notes"]


def test_bad_journey_fails_softly_and_can_be_retried(intake):
    app_module, db, svc, staff = intake
    draft = capture_item(staff)
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "NOPE", "date": _today()})
    d = staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]
    assert d["status"] == "failed" and "route" in d["error"].lower()
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    assert staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]["status"] == "done"


def test_retry_after_a_late_failure_never_duplicates_the_item(intake, monkeypatch):
    app_module, db, svc, staff = intake
    draft = capture_item(staff)
    real = svc.create_found

    def store_then_crash(*args, **kwargs):
        real(*args, **kwargs)                       # the item is stored ...
        raise RuntimeError("matching service down")  # ... then something after it fails
    monkeypatch.setattr(svc, "create_found", store_then_crash)
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    assert staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]["status"] == "failed"
    monkeypatch.setattr(svc, "create_found", real)
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    d = staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]
    assert d["status"] == "done" and db.found_reports.count_documents({"draftId": draft}) == 1
    assert staff.post(f"/api/intake/drafts/{draft}/finalize",
                      json={"route_id": "R100", "date": _today()}).status_code == 409   # already saved


def test_a_stuck_save_can_be_retried(intake):
    from datetime import timedelta
    app_module, db, svc, staff = intake
    draft = capture_item(staff)
    old = svc.clock() - timedelta(minutes=20)
    db.item_drafts.update_one({"draftId": draft}, {"$set": {"status": "analysing", "updatedAt": old}})
    resp = staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    assert resp.status_code == 202
    assert staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]["status"] == "done"


def test_id_numbers_are_masked_on_inside_frames_too(intake, monkeypatch):
    app_module, db, svc, staff = intake

    class InsideOCR:
        def extract_text(self, path):
            # inside frames are saved as i1.jpg, i2.jpg ...; only they "show" an ID card
            return "aadhaar 2345 6789 0124" if os.path.basename(path).startswith("i") else ""
    monkeypatch.setattr(svc, "ocr", InsideOCR())
    draft = new_draft(staff)
    for a, s in ((0, 0), (60, 60)):
        send(staff, draft, view(a, shift=s))
    assert send(staff, draft, view(90, colour=(200, 200, 200), text="2345 6789 0124"), step="inside")["kept"]
    staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    d = staff.get(f"/api/intake/drafts/{draft}").get_json()["draft"]
    assert d["facts"]["maskedIds"] == 1


def test_too_few_views_cannot_be_saved(intake):
    app_module, db, svc, staff = intake
    draft = new_draft(staff)
    send(staff, draft, view())
    resp = staff.post(f"/api/intake/drafts/{draft}/finalize", json={"route_id": "R100", "date": _today()})
    assert resp.status_code == 400 and resp.get_json()["error"]["code"] == "too_few_views"


def test_drafts_are_private_to_their_depot(intake):
    app_module, db, svc, staff = intake
    draft = new_draft(staff)
    db.depots.insert_one({"phone": "9000000099", "name": "Other Depot", "password": "x", "city": "Madurai"})
    other = app_module.app.test_client()
    with other.session_transaction() as s:
        s["depot_phone"], s["depot_name"] = "9000000099", "Other Depot"
    assert other.get(f"/api/intake/drafts/{draft}").status_code == 404
    anonymous = app_module.app.test_client()
    assert anonymous.post("/api/intake/drafts").status_code == 401
    assert anonymous.get("/depot/items/new").status_code == 302


def test_scan_page_renders_for_staff(intake):
    app_module, db, svc, staff = intake
    page = staff.get("/depot/items/new")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "Add New Lost Item" in html and "intake.js" in html
    assert "Add new lost item" in staff.get("/depot").get_data(as_text=True)
