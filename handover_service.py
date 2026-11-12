"""
handover_service.py — the counter check before a depot hands an item over.

State on the match document (all new fields; nothing existing is renamed):
  securityQuestions            stored questions (answer indices live ONLY here)
  securityStaffChecked         staff confirmed the photo-derived answers
  securityQuestionsExpireAt    24 h after generation; regenerated after that
  securityGeneratedBy          "llm" | "rules"
  securityInsufficient         fewer than 4 trustworthy questions
  securityAttempts             answer submissions so far
  securityFailedAttempts       submissions below the escalation mark
  securityPassed / securityPassedAt / securityPassedBy
  securityEscalated            2/4-type result: staff must call / check ID
  securityLocked               3 failed attempts
  securityAgreement            [AI guesses staff agreed with, checked] (quality metric)
  securityLog                  audit trail: generated, staff-checked, outcome, override

States reported to the depot page:
  staff_check   photo-derived questions wait for staff to confirm the answers
  ready         questions can be shown to the claimant
  passed        handover allowed
  escalated     call the passenger / check photo ID, then confirm as staff
  insufficient  not enough trustworthy questions: staff ID check required
  locked        three failed attempts: use the claims desk with photo ID
"""
from __future__ import annotations

import logging
import threading


from bson import ObjectId
from bson.errors import InvalidId

import handover_check as hc
from passenger_auth.timeutil import from_db, to_db, utcnow

logger = logging.getLogger(__name__)
LOG_LIMIT = 50


class HandoverError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


class HandoverService:
    def __init__(self, db, image_root, classifier, answer_key, claims_config, clock=utcnow, use_llm=True,
                 async_jobs=True):
        self.db = db
        self.matches = db["matches"]
        self.found = db["found_reports"]
        self.lost = db["lost_reports"]
        self.image_root = image_root
        self.classifier = classifier
        self.answer_key = answer_key
        self.claims_config = claims_config
        self.clock = clock
        self.use_llm = use_llm
        self.async_jobs = async_jobs

    # ── loading ────────────────────────────────────────────────────────────
    def match_for_depot(self, match_id, depot_phone):
        try:
            oid = ObjectId(str(match_id))
        except (InvalidId, TypeError):
            raise HandoverError(400, "invalid_match", "Invalid match id.")
        match = self.matches.find_one({"_id": oid, "depot_phone": depot_phone})
        if not match:
            raise HandoverError(404, "not_found", "Match not found.")
        return match

    def _log(self, match_id, event, **fields):
        entry = {"at": to_db(self.clock()), "event": event, **fields}
        self.matches.update_one({"_id": match_id}, {"$push": {"securityLog": {"$each": [entry], "$slice": -LOG_LIMIT}}})
        logger.info("Handover check %s: %s %s", match_id, event, fields or "")

    # ── generation ─────────────────────────────────────────────────────────
    def generate(self, match, reason="generated"):
        """Build (or rebuild) the proposed questions for a match; stores them."""
        found = self.found.find_one({"found_id": match.get("found_id")}) or {}
        lost = self.lost.find_one({"request_id": match.get("request_id")}) or {}
        result = hc.build_question_set(found, lost, self.image_root, classifier=self.classifier,
                                       private_attrs=found.get("private_attributes") or {},
                                       use_llm=self.use_llm)
        now = self.clock()
        fields = {"securityQuestions": result["questions"], "securityStaffChecked": False,
                  "securityQuestionsExpireAt": to_db(now + hc.QUESTION_TTL),
                  "securityGeneratedBy": result["source"], "securityInsufficient": result["insufficient"],
                  "securityGeneratedAt": to_db(now),
                  # Colours known to be on the item: never a WRONG option of a colour question.
                  "securityItemColours": self._item_colours(found, result)}
        if not hc.needs_staff_check(result["questions"]) and len(result["questions"]) >= hc.MIN_QUESTIONS:
            final, _agree = hc.apply_staff_check(result["questions"], {})
            fields.update(securityQuestions=final, securityStaffChecked=True,
                          securityInsufficient=len(final) < hc.MIN_QUESTIONS)
        self.matches.update_one({"_id": match["_id"]}, {"$set": fields})
        for k, v in (("securityAttempts", 0), ("securityFailedAttempts", 0), ("securityPassed", False),
                     ("securityEscalated", False), ("securityLocked", False)):
            self.matches.update_one({"_id": match["_id"], k: {"$exists": False}}, {"$set": {k: v}})
        self._log(match["_id"], reason, proposed=result["available"], source=result["source"],
                  insufficient=fields["securityInsufficient"])
        return self.matches.find_one({"_id": match["_id"]})

    @staticmethod
    def _item_colours(found, result) -> list:
        from claims.normalize import COLOUR_SET, tokens
        seen = [c for c, s in ((result.get("facts") or {}).get("visual", {}).get("colours") or {}).items()
                if s >= 0.08]
        seen += [c.get("name") for c in ((found.get("attributes") or {}).get("colours") or [])
                 if isinstance(c, dict) and c.get("name")]
        seen += [t for t in tokens(f"{found.get('notes') or ''} {found.get('public_description') or ''}")
                 if t in COLOUR_SET]
        return list(dict.fromkeys(c for c in seen if c))

    def pregenerate(self, match_id):
        """Called when a match is created: questions are ready before staff press resolve."""
        def job():
            try:
                match = self.matches.find_one({"_id": match_id})
                if match and not match.get("securityQuestions"):
                    self.generate(match, "pre-generated")
            except Exception:                              # never break matching for this
                logger.exception("Pre-generating handover questions for %s failed.", match_id)
        if self.async_jobs:
            threading.Thread(target=job, name=f"handover-{str(match_id)[:8]}", daemon=True).start()
        else:
            job()

    def _fresh(self, match):
        """The match with questions that are not expired (regenerated if needed)."""
        exp = match.get("securityQuestionsExpireAt")
        expired = exp is None or from_db(exp) <= self.clock()
        if (expired or "securityQuestions" not in match) and not match.get("securityPassed") \
                and not match.get("securityLocked"):
            return self.generate(match, "regenerated" if exp else "generated")
        return match

    # ── state for the depot page ───────────────────────────────────────────
    def state(self, match_id, depot_phone) -> dict:
        match = self._fresh(self.match_for_depot(match_id, depot_phone))
        base = {"attemptsLeft": max(0, hc.MAX_FAILED_ATTEMPTS - match.get("securityFailedAttempts", 0)),
                "matchId": str(match["_id"])}
        if match.get("securityPassed"):
            return {**base, "state": "passed"}
        if match.get("securityLocked"):
            return {**base, "state": "locked"}
        if match.get("securityEscalated"):
            return {**base, "state": "escalated"}
        questions = match.get("securityQuestions") or []
        if not match.get("securityStaffChecked"):
            # Staff confirm photo-derived answers, and add questions while holding the item
            # (also when the proposals are fewer than MIN_QUESTIONS).
            unchecked = hc.needs_staff_check(questions)
            return {**base, "state": "staff_check", "questions": hc.public_view(unchecked),
                    "confirmedCount": len(questions) - len(unchecked), "minQuestions": hc.MIN_QUESTIONS,
                    "addable": self.addable(match)}
        if len(questions) < hc.MIN_QUESTIONS:
            return {**base, "state": "insufficient"}
        return {**base, "state": "ready", "questions": hc.public_view(questions)}

    def addable(self, match) -> list:
        """Question kinds staff can add while holding the item (they pick the true value)."""
        found = self.found.find_one({"found_id": match.get("found_id")}) or {}
        category = found.get("questionCategory") or found.get("category") or "other"
        # Camera-proposed (unverified) questions stay addable: on candid photos the camera often
        # misreads (a black wallet as brown), and staff must be able to enter the true value.
        have = {q["attribute"] for q in match.get("securityQuestions") or [] if q.get("verified")}
        kinds = [("second_colour", "A second colour on the item (not the main one)",
                  sorted(c.title() for c in hc.COLOUR_LAB)),
                 ("main_colour", "The item's main colour", sorted(c.title() for c in hc.COLOUR_LAB)),
                 ("material", "What it is made of", list(hc.MATERIALS)),
                 ("pattern", "Its surface pattern", list(hc.PATTERNS))]
        if category in hc.STYLES:
            kinds.append(("style", "What kind of item it is", list(hc.STYLES[category])))
        return [{"attribute": a, "label": label, "values": vals} for a, label, vals in kinds if a not in have]

    # ── staff confirmation ─────────────────────────────────────────────────
    def staff_check(self, match_id, depot_phone, picks, added, staff_name):
        match = self._fresh(self.match_for_depot(match_id, depot_phone))
        if match.get("securityPassed") or match.get("securityLocked") or match.get("securityEscalated"):
            raise HandoverError(409, "not_checkable", "This check is already finished.")
        picks = {str(k): v for k, v in (picks or {}).items() if v is None or isinstance(v, int)}
        questions = list(match.get("securityQuestions") or [])
        found = self.found.find_one({"found_id": match.get("found_id")}) or {}
        category = found.get("questionCategory") or found.get("category") or "other"
        noun = (found.get("itemNoun") or hc.NOUNS.get(category, "item")).lower()
        added = [a for a in (added or [])[:6] if isinstance(a, dict) and isinstance(a.get("value"), str)
                 and a["value"].strip()]
        # What staff enter while holding the item replaces the camera's guess for that attribute.
        replaced = {a.get("attribute") for a in added}
        dropped = [q for q in questions if not q.get("verified") and q["attribute"] in replaced]
        questions = [q for q in questions if q not in dropped]
        for q in dropped:
            picks.pop(str(q["id"]), None)
        staff_colours = {a["attribute"]: a["value"].strip().lower() for a in added
                         if a.get("attribute") in ("main_colour", "second_colour")}
        on_item = list(match.get("securityItemColours") or []) + list(staff_colours.values())
        main_known = staff_colours.get("main_colour") or next(
            (str(q["options"][q.get("answerIndex", 0)]).lower() for q in questions
             if q["attribute"] == "main_colour" and q.get("verified")), "")
        for add in added:
            q = self._staff_question(add, category, noun, questions, on_item, main_known)
            if q:
                questions.append(q)
        final, (agreed, checked) = hc.apply_staff_check(questions, picks)
        enough = len(final) >= hc.MIN_QUESTIONS
        self.matches.update_one({"_id": match["_id"]}, {"$set": {
            "securityQuestions": final, "securityStaffChecked": True, "securityInsufficient": not enough},
            "$inc": {"securityAgreement.0": agreed, "securityAgreement.1": checked}})
        self._log(match["_id"], "staff-checked", by=str(staff_name or "")[:60], asked=len(final),
                  ai_agreed=agreed, ai_checked=checked, added=len(added or []))
        return self.state(match_id, depot_phone)

    def _staff_question(self, add, category, noun, existing, on_item=(), main_known=""):
        """A question staff added by picking the true value; options built to contrast with it."""
        if not isinstance(add, dict):
            return None
        attr, value = add.get("attribute"), add.get("value")
        if attr in {q["attribute"] for q in existing} or not isinstance(value, str):
            return None
        if attr in ("second_colour", "main_colour"):
            truth = value.strip().lower()
            if truth not in hc.COLOUR_LAB:
                return None
            others = [c for c in on_item if c in hc.COLOUR_LAB and c != truth]
            opts = hc._colour_options(truth, [truth] + others)           # nothing on the item is a wrong option
            # Name the main colour only when staff confirmed it (the camera's reading can be wrong).
            main = main_known if attr == "second_colour" and main_known and main_known != truth else ""
        elif attr == "material" and value in hc.MATERIALS:
            opts = hc._set_options(value, list(hc.MATERIALS), hc.MOST_COMMON["material"])
            main = ""
        elif attr == "pattern" and value in hc.PATTERNS:
            opts = hc._set_options(value, list(hc.PATTERNS), hc.MOST_COMMON["pattern"])
            main = ""
        elif attr == "style" and value in hc.STYLES.get(category, ()):
            opts = hc._set_options(value, list(hc.STYLES[category]), hc.STYLES[category][0])
            main = ""
        else:
            return None
        if not opts:
            return None
        text = hc.TEMPLATES[attr].format(noun=noun, main=main or "its main colour")
        if attr == "second_colour" and not main:
            text = f"Which colour, besides the main one, is on your {noun}?"
        return {"id": f"s{len(existing) + 1}", "attribute": attr, "text": text, "options": opts,
                "answerIndex": 0, "verified": True, "source": "staff"}

    # ── the claimant's answers ─────────────────────────────────────────────
    def submit(self, match_id, depot_phone, answers):
        match = self.match_for_depot(match_id, depot_phone)
        if match.get("securityPassed"):
            return {"result": "pass", "state": "passed", "attemptsLeft": 0}
        if match.get("securityLocked"):
            raise HandoverError(423, "locked", "Too many failed attempts. Use the claims desk with photo ID.")
        if match.get("securityEscalated"):
            raise HandoverError(409, "escalated", "This check needs a staff call / ID check now.")
        questions = match.get("securityQuestions") or []
        if not match.get("securityStaffChecked") or len(questions) < hc.MIN_QUESTIONS:
            raise HandoverError(409, "not_ready", "The questions are not ready yet.")
        exp = match.get("securityQuestionsExpireAt")
        if exp is not None and from_db(exp) <= self.clock():
            raise HandoverError(409, "expired", "These questions expired. Open the check again.")
        if not isinstance(answers, dict) or set(answers) != {q["id"] for q in questions}:
            raise HandoverError(400, "invalid_answers", "Answer every question.")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in answers.values()):
            raise HandoverError(400, "invalid_answers", "Answers must be option numbers.")
        found = self.found.find_one({"found_id": match.get("found_id")}) or {}
        correct, total = hc.score_answers(questions, answers, found.get("private_attributes") or {},
                                          self.answer_key, self.claims_config)
        result = hc.outcome(correct, total)
        attempts = match.get("securityAttempts", 0)
        now = to_db(self.clock())
        guard = {"_id": match["_id"], "securityAttempts": attempts, "securityPassed": {"$ne": True},
                 "securityLocked": {"$ne": True}}
        update = {"$inc": {"securityAttempts": 1}}
        if result == "pass":
            update["$set"] = {"securityPassed": True, "securityPassedAt": now, "securityPassedBy": "questions"}
        elif result == "escalate":
            update["$set"] = {"securityEscalated": True, "securityEscalatedAt": now}
        else:
            failed = match.get("securityFailedAttempts", 0) + 1
            update["$set"] = {"securityFailedAttempts": failed}
            if failed >= hc.MAX_FAILED_ATTEMPTS:
                update["$set"].update(securityLocked=True, securityLockedAt=now)
                result = "locked"
            else:
                # Next attempt: same facts, new option order (nothing learnt from positions).
                reshuffled, _a = hc.apply_staff_check([{**q, "verified": True} for q in questions], {})
                update["$set"]["securityQuestions"] = reshuffled
        res = self.matches.update_one(guard, update)
        if not res.modified_count:
            raise HandoverError(409, "stale", "These answers were already submitted.")
        # The score and which answers were wrong are never returned: only the outcome.
        self._log(match["_id"], "answered", outcome=result, correct=correct, total=total, attempt=attempts + 1)
        after = self.matches.find_one({"_id": match["_id"]})
        return {"result": result, "attemptsLeft": max(0, hc.MAX_FAILED_ATTEMPTS - after.get("securityFailedAttempts", 0)),
                "state": self.state(match_id, depot_phone)["state"]}

    # ── staff override after a call / ID check ─────────────────────────────
    def override(self, match_id, depot_phone, staff_name, id_checked, id_last4=None):
        match = self.match_for_depot(match_id, depot_phone)
        if match.get("securityPassed"):
            return {"state": "passed"}
        if match.get("securityLocked"):
            raise HandoverError(423, "locked", "Locked after three failed attempts. Use the claims desk.")
        allowed = match.get("securityEscalated") or (
            match.get("securityStaffChecked") and len(match.get("securityQuestions") or []) < hc.MIN_QUESTIONS)
        if not allowed:
            raise HandoverError(409, "not_allowed", "Staff verification is only for escalated or question-less checks.")
        name = str(staff_name or "").strip()
        if id_checked is not True or not 2 <= len(name) <= 60:
            raise HandoverError(400, "id_check_required", "Confirm the photo ID check and enter your name.")
        now = to_db(self.clock())
        self.matches.update_one({"_id": match["_id"]}, {"$set": {
            "securityPassed": True, "securityPassedAt": now, "securityPassedBy": f"staff:{name}"}})
        self._log(match["_id"], "staff-override", by=name,
                  reason="escalated" if match.get("securityEscalated") else "insufficient",
                  id_last4=(str(id_last4)[-4:] if id_last4 else None))
        return {"state": "passed"}


def resolve_allowed(match) -> bool:
    return bool(match.get("securityPassed"))


__all__ = ["HandoverService", "HandoverError", "resolve_allowed"]
