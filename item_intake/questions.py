"""
From analysis facts to the found-item record: private notes, a safe public
description, and the ownership questions (claims attributes).

Rules (Phase 2, Feature D):
  * only facts at or above INTAKE_ANSWER_MIN_CONFIDENCE become security answers;
    lower-confidence facts go into the staff-only notes, so a wrong AI guess can
    never lock the real owner out;
  * never the dominant colour or the category — those are in the public
    description and guessable (claims also marks such answers "leaked");
  * multiple-choice options are PLAUSIBLE: the true value plus the nearest
    neighbours in the same attribute (colour: closest in CIEDE2000; brand: other
    brands of the same category; contents: other common contents) — never
    colour-wheel opposites, which would give the answer away.
"""
import secrets

from vocabulary import BRANDS, CATEGORY_LABELS, similar_colours

_rng = secrets.SystemRandom()
COMMON_CONTENTS = ("phone", "wallet", "book", "bottle", "charger", "tiffin", "clothes", "earphones",
                   "keys", "medicine", "money", "towel", "umbrella", "spectacles")


def _pick(pool, exclude, n=2):
    options = [p for p in pool if p.casefold() not in {e.casefold() for e in exclude}]
    _rng.shuffle(options)
    return options[:n]


def public_description(facts) -> str:
    colour = facts["colours"][0]["name"].title() if facts.get("colours") else ""
    return f"{colour} {facts.get('noun') or 'item'}".strip()


def build_answers(facts: dict, min_conf: float) -> tuple:
    """
    Returns (raw_attributes, choices, report). raw_attributes go straight into
    claims.encode_private_attributes (hashed there); `report` records, per
    attribute, its confidence and whether it became a question — never the value.
    """
    category = facts.get("category") or "other"
    raw, choices, report = {}, {}, {}

    def consider(name, value, conf, options=None, source=None):
        used = value is not None and conf >= min_conf
        report[name] = {"confidence": round(float(conf), 3), "asQuestion": used, "source": source}
        if used:
            raw[name] = value
            if options:
                choices[name] = [value] + options

    detail = facts.get("detailColour")
    if detail and category in ("bag", "wallet", "clothing"):
        consider("zipper_tag_colour", detail["name"], detail["confidence"],
                 similar_colours(detail["name"], 2), "detail close-up")

    contents = facts.get("contents") or []
    if contents and category in ("bag", "wallet", "documents", "other"):
        top = contents[0]
        present = {c["name"] for c in contents}
        consider("content_item", top["name"], top["confidence"],
                 _pick(COMMON_CONTENTS, present | {top["name"]}), "inside view")

    brand = facts.get("brand")
    if brand:
        # Distractors come from the brand's own market (a bag brand gets other bag
        # brands), so "Wildcraft / Skybags / VIP" — never "Wildcraft / Acer / Redmi".
        home = next((c for c, names in BRANDS.items() if brand["name"] in names and len(names) >= 3), None)
        pool = BRANDS.get(home or category) or ()
        if len(pool) < 3:
            pool = BRANDS["bag"]
        consider("brand", brand["name"], brand["confidence"], _pick(pool, {brand["name"]}), "OCR")

    tag = facts.get("tagText")
    if tag:
        consider("tag_text", tag["text"], tag["confidence"], None, "tag close-up (OCR)")

    marks = [f for f in facts.get("features") or [] if f["name"] in ("keychain", "name tag", "sticker", "patch")]
    if marks:
        consider("distinguishing_mark", marks[0]["name"], marks[0]["confidence"], None, "outside views")
    return raw, choices, report


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
        lines.append(f"Brand: {facts['brand']['name']}.")
    if facts.get("tagText"):
        lines.append(f"Text on tag: \"{facts['tagText']['text']}\".")
    if facts.get("detailColour"):
        lines.append(f"Zip pull / mark colour: {facts['detailColour']['name']}.")
    if facts.get("maskedIds"):
        lines.append(f"An ID document was seen; its number was masked in {facts['maskedIds']} photo(s).")
    asked = sorted(k for k, v in report.items() if v["asQuestion"])
    lines.append("[Auto-logged by camera scan" + (f"; questions from: {', '.join(asked)}" if asked else "") + "]")
    return " ".join(line for line in lines if line)
