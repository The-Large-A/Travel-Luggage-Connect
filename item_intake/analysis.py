"""
Turn the kept scan frames into facts about the item.

Inputs are absolute image paths grouped by capture step:
    outside  — the turning sweep (front, sides, back, after the flip)
    inside   — the contents shown to the camera
    tag      — close-up stills of a tag / label / printed text
    detail   — close-up stills of a zip pull, sticker or other mark

Signals:
    OpenCV (vision.py) .... item mask, named colours, text regions
    CLIP zero-shot ........ category, material, external features, contents
    OCR ................... brand and the text on a tag
    captioner ............. one-sentence description

Every fact carries a confidence so the caller can decide what is safe to use
as a security answer. ID numbers found by OCR are masked in the stored image
and redacted from the text before anything is saved.
"""
import logging
import re

import cv2

from vocabulary import ALL_BRANDS, BRANDS, CONTENTS, EXTERNAL_FEATURES, MATERIALS

from . import vision

logger = logging.getLogger(__name__)

CATEGORY_PROMPTS = {
    "bag": "a bag, backpack or suitcase", "wallet": "a wallet or purse", "phone": "a mobile phone",
    "laptop": "a laptop computer", "spectacles": "a pair of spectacles", "umbrella": "an umbrella",
    "documents": "a file folder with documents", "clothing": "a piece of clothing", "other": "a small everyday object",
}
NOUNS = {"bag": "bag", "wallet": "wallet", "phone": "mobile phone", "laptop": "laptop",
         "spectacles": "pair of spectacles", "umbrella": "umbrella", "documents": "document folder",
         "clothing": "clothing item", "other": "item"}
CONTENT_PROMPTS = {
    "phone": "a mobile phone", "wallet": "a wallet", "spectacles": "spectacles", "keys": "a bunch of keys",
    "tiffin": "a steel tiffin box", "umbrella": "a folded umbrella", "book": "books or notebooks",
    "bottle": "a water bottle", "charger": "a phone or laptop charger", "earphones": "earphones",
    "money": "cash", "towel": "a towel", "clothes": "folded clothes", "medicine": "medicine strips",
    "laptop": "a laptop", "photo": "a photograph",
}
assert set(CONTENT_PROMPTS) == set(CONTENTS)
MARK_FEATURES = ("keychain", "name tag", "sticker", "patch")

ID_PATTERN = re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)")
ID_WORDS = re.compile(r"aadhaa?r|uidai|ஆதார்|government of india|भारत सरकार|vid\s*:", re.IGNORECASE)


def looks_like_id(text: str) -> bool:
    return bool(text) and bool(ID_PATTERN.search(text) or ID_WORDS.search(text))


def redact_ids(text: str) -> str:
    return ID_PATTERN.sub("[ID number masked]", text or "")


def _presence(classifier, paths, noun, thing):
    """P(thing is visible) from a 2-way CLIP comparison against the plain item."""
    res = classifier.zero_shot(paths, [f"{noun} with {thing}", noun], template="a photo of {}")
    return res[f"{noun} with {thing}"] if res else 0.0


def analyse(frames: dict, classifier, ocr, captioner, progress=lambda pct, stage: None) -> dict:
    outside = frames.get("outside") or []
    inside = frames.get("inside") or []
    tags = frames.get("tag") or []
    details = frames.get("detail") or []
    clip_ok = bool(classifier is not None and getattr(classifier, "available", False))
    facts = {"clip": clip_ok, "maskedIds": 0}

    # 1. Colours from the item pixels of every outside view (OpenCV).
    progress(15, "Reading colours")
    totals = {}
    for path in outside:
        img = cv2.imread(path)
        if img is None:
            continue
        for name, share, _lab in vision.dominant_colours(img, vision.item_mask(img)):
            totals[name] = totals.get(name, 0.0) + share
    n = max(1, len(outside))
    colours = sorted(({"name": k, "share": round(v / n, 3)} for k, v in totals.items()), key=lambda c: -c["share"])
    facts["colours"] = [c for c in colours if c["share"] >= 0.08][:3]

    # 2. What it is (CLIP zero-shot over the shared vocabulary).
    progress(35, "Recognising the item")
    category, cat_conf = "other", 0.0
    if clip_ok:
        res = classifier.zero_shot(outside, list(CATEGORY_PROMPTS.values()), template="a photo of {}") or {}
        if res:
            best = max(res, key=res.get)
            category = next(k for k, v in CATEGORY_PROMPTS.items() if v == best)
            cat_conf = res[best]
    facts["category"], facts["categoryConfidence"] = category, round(cat_conf, 3)
    noun = NOUNS[category]
    facts["noun"] = noun

    material, mat_conf, features = None, 0.0, []
    if clip_ok and category in ("bag", "wallet", "clothing", "other"):
        res = classifier.zero_shot(outside, [f"a {m} {noun}" for m in MATERIALS]) or {}
        if res:
            best = max(res, key=res.get)
            material, mat_conf = best.split(" ")[1], res[best]
        for feat in EXTERNAL_FEATURES:
            p = _presence(classifier, outside, f"a {noun}", feat)
            if p >= 0.6:
                features.append({"name": feat, "confidence": round(p, 3)})
    facts["material"] = material if mat_conf >= 0.35 else None
    facts["materialConfidence"] = round(mat_conf, 3)
    facts["features"] = sorted(features, key=lambda f: -f["confidence"])[:4]

    progress(50, "Checking the contents")
    contents = []
    if clip_ok and inside:
        for key, prompt in CONTENT_PROMPTS.items():
            p = _presence(classifier, inside, "an open bag", prompt)
            if p >= 0.55:
                contents.append({"name": key, "confidence": round(p, 3)})
    facts["contents"] = sorted(contents, key=lambda c: -c["confidence"])[:4]

    # 3. Zip pull / mark colour from the detail close-up (centre of the frame).
    detail_colour = None
    for path in details:
        img = cv2.imread(path)
        if img is None:
            continue
        cols = vision.dominant_colours(vision.centre_crop(img, 0.45))
        if cols:
            name, share, _ = cols[0]
            detail_colour = {"name": name, "confidence": round(min(0.95, share), 3), "source": path}
            break
    facts["detailColour"] = detail_colour

    # 4. Text: every stored image that OpenCV sees text on is read (close-ups
    #    always), and any ID number found is masked in the file before saving —
    #    an Aadhaar card inside a wallet must never be stored readable.
    progress(65, "Reading printed text")
    texts, tag_texts = [], []
    for path in (tags + details + outside + inside):
        img = cv2.imread(path)
        if img is None:
            continue
        boxes = vision.text_regions(img)
        if not boxes and path not in tags and path not in details:
            continue
        text = (ocr.extract_text(path) if ocr is not None else "") or ""
        if looks_like_id(text):
            cv2.imwrite(path, vision.blur_regions(img, boxes) if boxes else cv2.GaussianBlur(img, (51, 51), 0))
            facts["maskedIds"] += 1
        text = redact_ids(text).strip()
        if text and (path in tags or path in details or path == (outside[:1] or [None])[0]):
            texts.append(text)
            if path in tags:
                tag_texts.append(text)
    ocr_text = " ".join(texts).lower()
    facts["ocrText"] = ocr_text

    brand = None
    candidates = BRANDS.get(category) or ALL_BRANDS
    for b in sorted(set(candidates) | set(ALL_BRANDS), key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(b.lower())}(?![a-z])", ocr_text):
            brand = {"name": b, "confidence": 0.9 if b in candidates else 0.75}
            break
    facts["brand"] = brand

    tag_text = None
    if tag_texts:
        words = [w for w in re.findall(r"[^\W\d_]{2,}", " ".join(tag_texts), re.UNICODE)
                 if not brand or w.lower() not in brand["name"].lower().split()]
        if words and sum(len(w) for w in words) >= 3:
            tag_text = {"text": " ".join(words[:5])[:40], "confidence": 0.85}
    facts["tagText"] = tag_text

    # 5. One-sentence description.
    progress(80, "Writing the description")
    facts["caption"] = captioner.caption(outside[0], facts) if outside else ""
    facts["captionProvider"] = getattr(captioner, "last_provider", getattr(captioner, "name", "basic"))
    return facts
