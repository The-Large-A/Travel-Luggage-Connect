"""
From analysis facts to the found-item record: private notes, a safe public
description, and the ownership questions (claims attributes).

Rules (question bank v2):
  * only facts at or above INTAKE_ANSWER_MIN_CONFIDENCE become security answers;
    lower-confidence facts go into the staff-only notes, so a wrong AI guess can
    never lock the real owner out;
  * category gate: category-specific questions (zip pull, contents, brand,
    lock screen…) only when the camera is sure what the item is (confidence
    >= 0.8 AND 0.2 ahead of the runner-up). Otherwise the item is questioned as
    "other": only marks and tag text, which make sense for anything;
  * never the dominant colour or the category — those are in the public
    description and guessable (claims also marks such answers "leaked");
  * multiple-choice options CONTRAST (claims/options.py): colours an owner
    cannot mix up, about as common as the true one; contents that fit this
    kind of item and were not seen inside it; brands from the same market.
"""
from claims.attributes import ATTRIBUTES, applicable
from claims.options import build_choices
from vocabulary import CATEGORY_LABELS

CATEGORY_GATE_CONFIDENCE = 0.8
CATEGORY_GATE_MARGIN = 0.2

# What staff can type during a scan: facts the camera cannot see or read.
# "colour" goes to whichever colour question suits the item (zip pull, case, handle).
STAFF_DETAIL_FIELDS = ("content_item", "lock_screen", "tag_text", "distinguishing_mark", "colour")
_COLOUR_ATTR = {"bag": "zipper_tag_colour", "phone": "case_colour", "laptop": "case_colour",
                "spectacles": "case_colour", "umbrella": "handle_colour"}


def public_description(facts) -> str:
    colour = facts["colours"][0]["name"].title() if facts.get("colours") else ""
    return f"{colour} {facts.get('noun') or 'item'}".strip()


def question_category(facts) -> str:
    """The category questions are asked for: the camera's, or "other" when it was unsure."""
    category = facts.get("category") or "other"
    conf = float(facts.get("categoryConfidence") or 0.0)
    margin = facts.get("categoryMargin")
    margin = conf if margin is None else float(margin)          # older drafts: no runner-up recorded
    sure = conf >= CATEGORY_GATE_CONFIDENCE and margin >= CATEGORY_GATE_MARGIN
    return category if sure else "other"


def build_answers(facts: dict, min_conf: float, staff_details=None) -> tuple:
    """
    Returns (raw_attributes, choices, report, question_category). raw_attributes
    go straight into claims.encode_private_attributes (hashed there); `report`
    records, per attribute, its confidence and whether it became a question —
    never the value.
    """
    category = facts.get("category") or "other"
    qcat = question_category(facts)
    allowed = set(applicable(qcat))
    raw, report = {}, {}

    def consider(name, value, conf, source=None):
        fits = name in allowed
        used = value is not None and conf >= min_conf and fits
        report[name] = {"confidence": round(float(conf), 3), "asQuestion": used, "source": source}
        if not fits:
            report[name]["skipped"] = "does not suit this item" if qcat == category else "category uncertain"
        if used:
            raw[name] = value

    detail = facts.get("detailColour")
    if detail:
        consider("zipper_tag_colour", detail["name"], detail["confidence"], "detail close-up")

    contents = facts.get("contents") or []
    if contents:
        consider("content_item", contents[0]["name"], contents[0]["confidence"], "inside view")

    brand = facts.get("brand")
    if brand:
        consider("brand", brand["name"], brand["confidence"], "logo" if brand.get("source") == "logo" else "OCR")

    tag = facts.get("tagText")
    if tag:
        consider("tag_text", tag["text"], tag["confidence"], "tag close-up (OCR)")

    marks = [f for f in facts.get("features") or [] if f["name"] in ("keychain", "name tag", "sticker", "patch")]
    if marks:
        consider("distinguishing_mark", marks[0]["name"], marks[0]["confidence"], "outside views")

    # Details staff typed while scanning: what the camera cannot know. They replace a
    # camera guess for the same question; one that does not suit this item is reported.
    for field, value in (staff_details or {}).items():
        name = _COLOUR_ATTR.get(qcat) if field == "colour" else field
        if not name:
            report[f"staff_{field}"] = {"confidence": 1.0, "asQuestion": False, "source": "staff",
                                        "skipped": "does not suit this item"}
            continue
        consider(name, value, 1.0, "staff")

    seen_inside = [c["name"] for c in contents]
    raw, choices = build_choices(raw, qcat, {k: a["kind"] for k, a in ATTRIBUTES.items()},
                                 exclude_contents=seen_inside)
    return raw, choices, report, qcat


def private_notes(facts: dict, report: dict) -> str:
    """Staff-only description; also what the text matcher (SBERT) compares with lost reports."""
    lines = [facts.get("caption") or ""]
    label = CATEGORY_LABELS.get(facts.get("category") or "other", ("Item",))[0]
    lines.append(f"Category: {label} ({round(100 * facts.get('categoryConfidence', 0))}% sure).")
    if facts.get("colours"):
        lines.append("Colours: " + ", ".join(f"{c['name']} {round(100 * c['share'])}%" for c in facts["colours"]) + ".")
    if facts.get("material"):
        lines.append(f"Material: {facts['material']}.")
    if facts.get("features"):
        lines.append("Features: " + ", ".join(f["name"] for f in facts["features"]) + ".")
    if facts.get("contents"):
        lines.append("Inside: " + ", ".join(c["name"] for c in facts["contents"]) + ".")
    if facts.get("brand"):
        how = " (recognised from its logo)" if facts["brand"].get("source") == "logo" else ""
        lines.append(f"Brand: {facts['brand']['name']}{how}.")
    if facts.get("tagText"):
        lines.append(f"Text on tag: \"{facts['tagText']['text']}\".")
    if facts.get("detailColour"):
        lines.append(f"Zip pull / mark colour: {facts['detailColour']['name']}.")
    if facts.get("maskedIds"):
        lines.append(f"An ID document was seen; its number was masked in {facts['maskedIds']} photo(s).")
    asked = sorted(k for k, v in report.items() if v["asQuestion"])
    lines.append("[Auto-logged by camera scan" + (f"; questions from: {', '.join(asked)}" if asked else "") + "]")
    return " ".join(line for line in lines if line)
