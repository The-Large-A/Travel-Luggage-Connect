"""
Depot smart intake (Phase 2, Feature C): one button, one camera session, automatic saving.

item_drafts document:
    draftId, depotPhone, status (capturing | analysing | done | failed),
    frames {outside: [...], inside: [...]}  — kept views: {path, sharpness, phash}
    closeups [{kind: tag | detail, path}], journey {route_id, trip_id, date, found_seat_area},
    progress {pct, stage}, facts, result {foundId, qrTagId, matches}, error,
    createdAt, updatedAt, purgeAt (TTL)

The browser streams ~2 frames a second while staff turn the item. Each frame is
gated by OpenCV (sharp? well exposed? a NEW view?) and only the keepers are
stored, so a 30-second sweep becomes 6-8 distinct keyframes. Finalising runs
analysis + saving in a background job; the page polls the draft for progress.
"""
import logging
import os
import shutil
import threading
import uuid
from datetime import timedelta

from passenger_auth.timeutil import from_db, iso_utc, to_db

from . import vision
from .analysis import NOUNS, analyse
from .questions import STAFF_DETAIL_FIELDS, build_answers, private_notes, public_description

logger = logging.getLogger(__name__)

DRAFTS = "item_drafts"
STEPS = ("outside", "inside")
CLOSEUP_KINDS = ("tag", "detail")
STALE_JOB_MINUTES = 15            # an "analysing" draft untouched this long died with its process


class IntakeError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class IntakeService:
    def __init__(self, db, config, clock, upload_root, classifier, ocr, captioner, create_found, run_in_app):
        self.db = db
        self.drafts = db[DRAFTS]
        self.config = config
        self.clock = clock
        self.upload_root = upload_root          # .../static
        self.classifier = classifier            # CLIP wrapper with zero_shot(), or None
        self.ocr = ocr
        self.captioner = captioner
        self.create_found = create_found        # app.create_found_report
        self.run_in_app = run_in_app            # wraps a callable in the Flask app context
        self.async_jobs = config.async_jobs

    # ── paths ────────────────────────────────────────────────────────────────
    def _rel_dir(self, draft_id):
        return f"uploads/intake/{draft_id}"

    def _abs(self, rel):
        return os.path.join(self.upload_root, *rel.split("/"))

    def _save(self, draft_id, name, img, quality=85):
        rel = f"{self._rel_dir(draft_id)}/{name}.jpg"
        os.makedirs(os.path.dirname(self._abs(rel)), exist_ok=True)
        with open(self._abs(rel), "wb") as f:
            f.write(vision.encode_jpeg(img, quality))
        return rel

    # ── drafts ───────────────────────────────────────────────────────────────
    def create(self, depot) -> dict:
        now = self.clock()
        doc = {"draftId": uuid.uuid4().hex, "depotPhone": depot["phone"], "status": "capturing",
               "frames": {s: [] for s in STEPS}, "closeups": [], "journey": None,
               "progress": {"pct": 0, "stage": "Capturing"}, "facts": None, "result": None, "error": None,
               "createdAt": to_db(now), "updatedAt": to_db(now),
               "purgeAt": to_db(now + timedelta(hours=self.config.draft_ttl_hours))}
        self.drafts.insert_one(doc)
        return doc

    def get(self, draft_id, depot_phone) -> dict:
        doc = self.drafts.find_one({"draftId": draft_id, "depotPhone": depot_phone}) \
            if isinstance(draft_id, str) and len(draft_id) <= 64 else None
        if not doc:
            raise IntakeError(404, "not_found", "This scan was not found (it may have expired).")
        return doc

    def _stale(self, draft) -> bool:
        return draft["status"] == "analysing" and from_db(draft["updatedAt"]) < \
            self.clock() - timedelta(minutes=STALE_JOB_MINUTES)

    def _require_capturing(self, draft):
        if draft["status"] == "done":
            raise IntakeError(409, "already_saved", "This item has already been saved.")
        if draft["status"] not in ("capturing", "failed") and not self._stale(draft):
            raise IntakeError(409, "not_capturing", "This scan is already being saved.")

    # ── frames ───────────────────────────────────────────────────────────────
    def add_frame(self, draft, step, data: bytes) -> dict:
        """Gate one streamed frame. Returns live feedback for the camera screen."""
        self._require_capturing(draft)
        if step not in STEPS:
            raise IntakeError(400, "invalid_step", "step must be 'outside' or 'inside'.")
        img = vision.decode(data)
        if img is None:
            raise IntakeError(400, "not_an_image", "That frame could not be read as an image.")
        img = vision.resize_max(img, 1280)
        cfg = self.config
        kept_frames = draft["frames"][step]
        limit = cfg.max_views if step == "outside" else cfg.max_inside
        feedback = self._counts(draft)
        sharp = vision.sharpness(img)
        exp = vision.exposure(img)
        # Too dark only when genuinely black (a dark wallet in dim light is still readable).
        if exp["mean"] < cfg.min_brightness and (exp["dark"] > 0.25 or exp["mean"] < cfg.min_brightness / 2):
            return {**feedback, "kept": False, "hint": "Too dark — move to better light."}
        # Glare on the item, not a white table or sheet behind it, rejects a frame.
        if exp["mean"] > cfg.max_brightness or exp["glare"] > 0.08:
            return {**feedback, "kept": False, "hint": "Too bright — avoid direct light or glare."}
        if sharp < cfg.min_sharpness:
            return {**feedback, "kept": False, "hint": "Hold still — the picture is blurry."}
        if len(kept_frames) >= limit:
            return {**feedback, "kept": False, "full": True, "hint": "Enough views here — go to the next step."}
        h = vision.phash(img)
        if any(vision.hamming(h, int(f["phash"], 16)) < cfg.min_view_distance for f in kept_frames):
            return {**feedback, "kept": False, "hint": "Turn the item to show a new side."}
        rel = self._save(draft["draftId"], f"{step[0]}{len(kept_frames) + 1}", img)
        entry = {"path": rel, "sharpness": round(sharp, 1), "phash": f"{h:016x}"}
        res = self.drafts.update_one(
            {"_id": draft["_id"], f"frames.{step}.{limit - 1}": {"$exists": False}},
            {"$push": {f"frames.{step}": entry}, "$set": {"updatedAt": to_db(self.clock())}})
        if not res.modified_count:
            return {**feedback, "kept": False, "full": True, "hint": "Enough views here — go to the next step."}
        draft["frames"][step].append(entry)
        text = step == "outside" and bool(vision.text_regions(img))
        return {**self._counts(draft), "kept": True, "textDetected": text,
                "hint": "Text spotted — add a tag close-up after the sweep." if text else "Good view captured."}

    def add_closeup(self, draft, kind, data: bytes) -> dict:
        self._require_capturing(draft)
        if kind not in CLOSEUP_KINDS:
            raise IntakeError(400, "invalid_kind", "kind must be 'tag' or 'detail'.")
        if len(draft["closeups"]) >= self.config.max_closeups:
            raise IntakeError(409, "too_many", "Up to %d close-ups per item." % self.config.max_closeups)
        img = vision.decode(data)
        if img is None:
            raise IntakeError(400, "not_an_image", "That photo could not be read as an image.")
        if vision.sharpness(img) < self.config.min_sharpness * 0.6:
            return {**self._counts(draft), "kept": False, "hint": "Too blurry for reading text — retake it closer and steadier."}
        rel = self._save(draft["draftId"], f"{kind}{len(draft['closeups']) + 1}", vision.resize_max(img, 1600), 90)
        self.drafts.update_one({"_id": draft["_id"]}, {"$push": {"closeups": {"kind": kind, "path": rel}},
                                                       "$set": {"updatedAt": to_db(self.clock())}})
        draft["closeups"].append({"kind": kind, "path": rel})
        return {**self._counts(draft), "kept": True, "hint": "Close-up saved."}

    def _counts(self, draft):
        return {"views": len(draft["frames"]["outside"]), "target": self.config.target_views,
                "inside": len(draft["frames"]["inside"]), "closeups": len(draft["closeups"])}

    # ── finalise (automatic saving) ──────────────────────────────────────────
    def finalize(self, draft, depot, journey: dict) -> dict:
        """
        `journey` also carries what staff can add that the camera cannot see: the
        item type ("category", optional) and ownership details ("detail_*": what
        was inside, the lock screen, a name tag, a mark, the zip/case/handle
        colour). The details are secrets: they live only in this request and the
        job, are hashed into the item's questions, and are never written raw to
        the draft or anywhere else.
        """
        self._require_capturing(draft)
        from claims.attributes import CATEGORIES
        staff_category = journey.get("category") if journey.get("category") in CATEGORIES else None
        staff_details = {k: str(journey.get(f"detail_{k}")).strip()[:80] for k in STAFF_DETAIL_FIELDS
                         if isinstance(journey.get(f"detail_{k}"), str) and journey.get(f"detail_{k}").strip()}
        if len(draft["frames"]["outside"]) < self.config.min_views_to_save:
            raise IntakeError(400, "too_few_views",
                              f"Capture at least {self.config.min_views_to_save} different views of the item first.")
        journey = {k: (str(journey.get(k)).strip() if journey.get(k) not in (None, "") else None)
                   for k in ("route_id", "trip_id", "date", "found_seat_area")}
        if not journey["route_id"] or not journey["date"]:
            raise IntakeError(400, "journey_required", "Choose the route and the date the item was found.")
        retryable = [{"status": {"$in": ["capturing", "failed"]}},
                     {"status": "analysing",
                      "updatedAt": {"$lt": to_db(self.clock() - timedelta(minutes=STALE_JOB_MINUTES))}}]
        claimed = self.drafts.find_one_and_update(
            {"_id": draft["_id"], "$or": retryable},
            {"$set": {"status": "analysing", "journey": journey, "error": None,
                      "progress": {"pct": 5, "stage": "Starting"}, "updatedAt": to_db(self.clock())}})
        if claimed is None:
            raise IntakeError(409, "not_capturing", "This scan is already being saved.")
        job = self.run_in_app(lambda: self._job(draft["draftId"], depot, journey, staff_category, staff_details))
        if self.async_jobs:
            threading.Thread(target=job, name=f"intake-{draft['draftId'][:8]}", daemon=True).start()
        else:
            job()
        return self.drafts.find_one({"_id": draft["_id"]})

    def _progress(self, draft_id, pct, stage):
        self.drafts.update_one({"draftId": draft_id}, {"$set": {"progress": {"pct": pct, "stage": stage},
                                                                "updatedAt": to_db(self.clock())}})

    def _job(self, draft_id, depot, journey, staff_category=None, staff_details=None):
        draft = self.drafts.find_one({"draftId": draft_id})
        try:
            # A retry after a failure that happened AFTER the item was stored (e.g.
            # during matching) must finish that item, never create a second one.
            existing = self.db["found_reports"].find_one({"draftId": draft_id})
            if existing:
                self._mark_done(draft_id, existing, None, (draft.get("facts") or {}), {})
                return
            frames = {step: [self._abs(f["path"]) for f in draft["frames"][step]] for step in STEPS}
            for kind in CLOSEUP_KINDS:
                frames[kind] = [self._abs(c["path"]) for c in draft["closeups"] if c["kind"] == kind]
            facts = analyse(frames, self.classifier, self.ocr, self.captioner,
                            progress=lambda pct, stage: self._progress(draft_id, pct, stage))
            if staff_category:                        # staff looked at the item: their word beats the camera's
                facts.update(category=staff_category, categoryConfidence=1.0, categoryMargin=1.0,
                             noun=NOUNS[staff_category], categorySource="staff")
            raw, choices, report, qcat = build_answers(facts, self.config.answer_min_confidence,
                                                       staff_details=staff_details)
            self._progress(draft_id, 92, "Saving and matching")
            images = [f["path"] for f in draft["frames"]["outside"]] + [f["path"] for f in draft["frames"]["inside"]]
            images += [c["path"] for c in draft["closeups"]]
            found, n = self.create_found(
                depot,
                {**journey, "notes": private_notes(facts, report), "public_description": public_description(facts),
                 "category": facts["category"]},
                image_paths=images, raw_attributes=raw, attribute_choices=choices,
                extra={"captureMode": "camera_scan", "draftId": draft_id, "ocr_text": facts.get("ocrText", ""),
                       "questionCategory": qcat, "itemNoun": facts.get("noun"),
                       "attributes": {
                           "category": {"value": facts["category"], "confidence": facts["categoryConfidence"]},
                           "colours": facts.get("colours"), "material": facts.get("material"),
                           "features": [f["name"] for f in facts.get("features") or []],
                           "captionProvider": facts.get("captionProvider"),
                           "questions": report,          # confidence per attribute — never the answer
                       }})
            self._mark_done(draft_id, found, n, facts, report)
            logger.info("Camera scan %s saved as item %s (%d match(es)).", draft_id, found["found_id"], n)
        except Exception as e:                       # includes FoundReportError (bad journey)
            message = str(e) if e.__class__.__name__ == "FoundReportError" else \
                "Something went wrong while saving. Your photos are kept — try saving again."
            if e.__class__.__name__ != "FoundReportError":
                logger.exception("Camera scan %s failed.", draft_id)
            self.drafts.update_one({"draftId": draft_id}, {"$set": {
                "status": "failed", "error": message, "progress": {"pct": 0, "stage": "Failed"},
                "updatedAt": to_db(self.clock())}})

    def _mark_done(self, draft_id, found, matches, facts, report):
        if matches is None:
            matches = self.db["matches"].count_documents({"itemId": found["found_id"]})
        from claims.attributes import HIDDEN, MIN_QUESTIONS, askable
        asked = sorted(askable(found))                 # what passengers will really be asked
        ready = len(asked) >= MIN_QUESTIONS
        self.drafts.update_one({"draftId": draft_id}, {"$set": {
            "status": "done", "progress": {"pct": 100, "stage": "Saved"}, "error": None,
            "facts": {k: facts.get(k) for k in ("category", "categoryConfidence", "colours", "material",
                                                "features", "captionProvider", "maskedIds")},
            "result": {"foundId": found["found_id"], "qrTagId": found.get("qrTagId"), "matches": matches,
                       "publicDescription": found.get("public_description"), "questions": asked,
                       "questionsReady": ready, "hiddenQuestion": bool(HIDDEN & set(asked))},
            "updatedAt": to_db(self.clock())}})

    # ── views ────────────────────────────────────────────────────────────────
    def view(self, draft) -> dict:
        return {"draftId": draft["draftId"], "status": draft["status"], "progress": draft.get("progress"),
                **self._counts(draft),
                "frames": {s: [f["path"] for f in draft["frames"][s]] for s in STEPS},
                "closeupPaths": [c["path"] for c in draft["closeups"]],
                "facts": draft.get("facts"), "result": draft.get("result"), "error": draft.get("error"),
                "updatedAt": iso_utc(draft["updatedAt"])}

    def discard(self, draft):
        if draft["status"] == "analysing" and not self._stale(draft):
            raise IntakeError(409, "busy", "This scan is being saved and cannot be discarded now.")
        saved = draft["status"] == "done" or self.db["found_reports"].find_one({"draftId": draft["draftId"]})
        if not saved:                         # a saved item still points at these photos
            shutil.rmtree(self._abs(self._rel_dir(draft["draftId"])), ignore_errors=True)
        self.drafts.delete_one({"_id": draft["_id"]})


def ensure_intake_indexes(db):
    specs = ((DRAFTS, [("draftId", 1)], {"unique": True}), (DRAFTS, [("purgeAt", 1)], {"expireAfterSeconds": 0}),
             (DRAFTS, [("depotPhone", 1), ("status", 1)], {}),
             # one found item per scan, even if a save is retried
             ("found_reports", [("draftId", 1)], {"unique": True,
                                                  "partialFilterExpression": {"draftId": {"$type": "string"}}}))
    for name, keys, opts in specs:
        try:
            db[name].create_index(keys, **opts)
        except Exception as e:
            logger.warning("Index creation skipped for %s %s: %s", name, keys, e)
