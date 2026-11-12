"""
handover_check.py — the anti-fraud gate at the depot counter.

When depot staff are about to hand a found item to someone who says it is
theirs (the green "resolve" button on the depot dashboard), the claimant must
answer multiple-choice questions about the item that only its owner can know.

Where the answers come from (the design rule that makes this anti-fraud):
  * The CORRECT answer always comes from the item the depot is holding: its
    photos (OpenCV + CLIP), the depot's private notes, the camera scan and the
    secret details staff recorded. Never from the claimant's own lost report:
    a fraudster knows what they wrote.
  * Anything the claimant already stated, or that the public notification
    told them ("Black backpack"), is never asked.

Image analysis (per found-item photo):
  * OpenCV: item mask (GrabCut), K-means colours in HSV (hue on a circle),
    edge density, shape complexity (solidity, polygon corners), aspect ratio,
    brightness and texture (Laplacian variance). Colours are named in CIE Lab
    with the shared palette of named shades.
  * CLIP zero-shot (the model already loaded by the app): material, style,
    pattern and features, averaged over every view, accepted only when CLIP is
    clearly sure. Groq currently offers no vision model; photos never leave the
    server.

Accuracy before the claimant sees anything: CLIP is a category model, and on
real photos its fine-grained guesses are often wrong (a red school backpack
read as a "duffel bag" with 78%, a "telescopic handle" at 88% on a bag that has
none). So every answer that came only from photo analysis is CONFIRMED BY STAFF
holding the item: they tap the correct option, or skip a question they cannot
tell. The browser is never told the AI's guess; staff pick independently and
the agreement rate is recorded. Facts staff typed themselves (secret details,
notes) and OCR text need no confirmation.

Options: four per question. The true value plus three distractors of the same
kind that the owner cannot confuse with it (no near-synonyms: colours that are
not "close shades" of each other, ΔE00 >= 25, material groups instead of
synonyms like nylon/polyester), never another value the item really has, and
always including the most common wrong guess for that kind of question (black
for a colour, fabric for a material, plain for a pattern, backpack for a bag).
The LLM (Groq, GROQ_REASONING_MODEL) words the questions and may suggest
distractors; every suggestion must pass the same checks or is ignored, and the
correct answer is never chosen by the LLM.

Scoring: 4-6 questions. At least 75% right (3/4, 4/5, 5/6) passes; at least
50% escalates to a staff call (phone + photo ID); less is a failed attempt.
Three failed attempts lock the check. The correct option index is stored only
in the match document; the browser never receives it, and only the outcome
(never the score or which answer was wrong) is returned.

Blind guessing on four-option questions passes 3/4 about 5% of the time per
attempt; 4/5 about 1.6%; 5/6 about 0.5%. That is why up to six questions are
asked when the item allows, why a 2/4 goes to a human instead of a retry, and
why hidden facts (staff-recorded secrets, contents) are preferred.
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
from datetime import timedelta

import cv2
import numpy as np

from claims.normalize import COLOUR_SET, normalize_text, tokens
from vocabulary import (BRAND_HOMES, BRANDS, COLOUR_LAB, CONTENT_POOLS, colours_confusable,
                        contrasting_colours, delta_e2000, nearest_colour, option_flatness)

logger = logging.getLogger(__name__)

QUESTION_TTL = timedelta(hours=24)
MIN_QUESTIONS = 4
MAX_QUESTIONS = 6
N_OPTIONS = 4
MAX_FAILED_ATTEMPTS = 3
PASS_SHARE = 0.75
ESCALATE_SHARE = 0.50
CLIP_PROPOSE_PROB = 0.35        # a CLIP guess is worth PROPOSING (staff confirm it) above this
FEATURE_PROPOSE = 0.6           # a feature is proposed as present above this 2-way probability
MAX_CANDIDATES = 8              # proposed before staff confirmation; at most MAX_QUESTIONS are asked
_rng = secrets.SystemRandom()

NOUNS = {"bag": "bag", "wallet": "wallet", "phone": "phone", "laptop": "laptop", "spectacles": "spectacles",
         "umbrella": "umbrella", "documents": "folder", "clothing": "clothing item", "other": "item"}

# Value sets per question kind. Each set is a list of mutually distinct values (no
# synonyms); the first entry is the most common guess for that kind of question.
MATERIALS = {"Fabric (nylon / cloth)": ("nylon", "polyester", "cloth", "fabric", "canvas"),
             "Leather": ("leather", "rexine"), "Plastic": ("plastic",), "Metal": ("metal",),
             "Denim": ("denim",), "Jute": ("jute",)}
PATTERNS = ("Plain, one colour", "Striped", "Checked", "Printed design", "Camouflage")
STYLES = {
    "bag": ("Backpack", "Wheeled trolley suitcase", "Shoulder / sling bag", "Handbag", "Duffel bag"),
    "wallet": ("Folding wallet", "Long wallet", "Zip pouch", "Clutch"),
    "umbrella": ("Folding umbrella", "Long walking-stick umbrella"),
    "spectacles": ("Clear (power) glasses", "Sunglasses"),
    "clothing": ("Shirt", "Jacket", "Sweater", "Shawl", "Saree"),
}
FEATURES = {
    "bag": ("Shoulder strap", "Wheels", "Telescopic handle", "Carry handle on top", "Keychain", "Name tag"),
    "wallet": ("Zip", "Snap button", "Keychain", "Card window"),
    "umbrella": ("Curved hook handle", "Strap with a button", "Wooden handle"),
    "laptop": ("Stickers on the lid", "A sleeve or cover"),
    "phone": ("A case or cover", "A cracked screen", "Stickers"),
}
MOST_COMMON = {"material": "Fabric (nylon / cloth)", "pattern": "Plain, one colour", "colour": "black",
               "content_item": "clothes"}

TEMPLATES = {
    "main_colour": "What is the main colour of your {noun}?",
    "second_colour": "Apart from {main}, which other colour is on your {noun}?",
    "material": "What is your {noun} made of?",
    "pattern": "What does the surface of your {noun} look like?",
    "style": "What kind of {noun} is it?",
    "feature": "Which of these does your {noun} have?",
    "brand": "Which brand is your {noun}?",
    "content_item": "Which of these was inside your {noun}?",
}


# ── image analysis ───────────────────────────────────────────────────────────
def visual_facts(path):
    """OpenCV measurements of the item in one photo, or None if unreadable."""
    from item_intake import vision
    img = cv2.imread(path) if path and os.path.exists(path) else None
    if img is None:
        return None
    img = vision.resize_max(img, 480)
    mask = vision.item_mask(img)
    sel = mask > 0
    if sel.sum() < 500:
        sel = np.ones(mask.shape, bool)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    px = hsv[sel].astype(np.float32)
    if len(px) > 8000:
        px = px[np.random.default_rng(7).choice(len(px), 8000, replace=False)]
    # Hue is an angle: cluster on (S·cos h, S·sin h, V) so red at 179 and 0 is one colour.
    ang = px[:, 0] * (2 * np.pi / 180.0)
    s, v = px[:, 1] / 255.0, px[:, 2] / 255.0
    feats = np.stack([s * np.cos(ang), s * np.sin(ang), v], axis=1).astype(np.float32)
    k = 4
    cv2.setRNGSeed(7)
    _c, labels, centres = cv2.kmeans(feats, k, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3),
                                     3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.ravel(), minlength=k).astype(float)
    colours = {}
    for (x, y, val), n in zip(centres, counts):
        sat = float(np.hypot(x, y))
        hue = (math.degrees(math.atan2(y, x)) % 360) / 2.0                      # back to OpenCV 0-180
        bgr = cv2.cvtColor(np.uint8([[[round(hue), round(min(1, sat) * 255), round(min(1, val) * 255)]]]),
                           cv2.COLOR_HSV2BGR)[0, 0]
        lab = cv2.cvtColor(np.uint8([[bgr]]), cv2.COLOR_BGR2LAB)[0, 0].astype(float)
        name, _d = nearest_colour((lab[0] * 100 / 255, lab[1] - 128, lab[2] - 128))
        colours[name] = colours.get(name, 0.0) + n / counts.sum()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    m8 = sel.astype(np.uint8)
    contours, _h = cv2.findContours(m8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    shape = {}
    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        hull = cv2.contourArea(cv2.convexHull(c)) or 1.0
        x, y, w, h = cv2.boundingRect(c)
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        shape = {"solidity": round(area / hull, 3), "corners": int(len(approx)), "aspect": round(w / max(h, 1), 3)}
    return {
        "colours": {k: round(v, 3) for k, v in sorted(colours.items(), key=lambda kv: -kv[1])},
        "edgeDensity": round(float((edges[sel] > 0).mean()), 4),
        "brightness": round(float(v.mean()), 3),
        "texture": round(float(cv2.Laplacian(gray, cv2.CV_64F)[sel].var()), 1),
        **shape,
    }


def _clip_choice(classifier, paths, labels):
    """Best of `labels` averaged over all views, if CLIP is clearly sure; else None."""
    if classifier is None or not getattr(classifier, "available", False) or not paths:
        return None
    res = classifier.zero_shot(paths, labels, template="a photo of {}") or {}
    if not res:
        return None
    ranked = sorted(res.items(), key=lambda kv: -kv[1])
    (top, p1), p2 = ranked[0], ranked[1][1] if len(ranked) > 1 else 0.0
    return (top, p1) if p1 >= CLIP_PROPOSE_PROB and p1 > p2 else None


def _clip_presence(classifier, paths, noun, thing):
    res = classifier.zero_shot(paths, [f"a {noun} with {thing.lower()}", f"a {noun}"], template="a photo of {}")
    return res[f"a {noun} with {thing.lower()}"] if res else 0.5


def semantic_facts(classifier, paths, category):
    """CLIP: material, pattern, style and features of the item, only where clearly sure."""
    facts = {}
    if classifier is None or not getattr(classifier, "available", False) or not paths:
        return facts
    noun = NOUNS.get(category, "item")
    if category in ("bag", "wallet", "clothing", "other"):
        labels = {f"a {noun} made of {next(iter(words))}": group for group, words in MATERIALS.items()}
        hit = _clip_choice(classifier, paths, list(labels))
        if hit:
            facts["material"] = (labels[hit[0]], hit[1])
    pattern_labels = {"a plain single-coloured " + noun: PATTERNS[0], "a striped " + noun: PATTERNS[1],
                      "a checked " + noun: PATTERNS[2], f"a {noun} with a printed design": PATTERNS[3],
                      "a camouflage " + noun: PATTERNS[4]}
    hit = _clip_choice(classifier, paths, list(pattern_labels))
    if hit:
        facts["pattern"] = (pattern_labels[hit[0]], hit[1])
    if category in STYLES:
        labels = {f"a {s.lower()}": s for s in STYLES[category]}
        hit = _clip_choice(classifier, paths, list(labels))
        if hit:
            facts["style"] = (labels[hit[0]], hit[1])
    if category in FEATURES:
        probs = {f: _clip_presence(classifier, paths, noun, f) for f in FEATURES[category]}
        facts["features"] = probs
    return facts


# ── facts → candidate questions ──────────────────────────────────────────────
def _known_to_claimant(lost, found) -> set:
    """Folded words the claimant already has: their own report and the public description."""
    text = f"{lost.get('description') or ''} {lost.get('descriptionEnhanced') or ''} " \
           f"{found.get('public_description') or ''}"
    return set(tokens(text)) | set(normalize_text(text).split())


def _colour_options(truth, item_colours):
    """Truth + 3 colours the owner cannot confuse with it, none of them really on the item."""
    on_item = set(item_colours)
    blocked = {c for c in COLOUR_LAB if any(c == o or colours_confusable(c, o) for o in on_item | {truth})}
    picks = []
    common = MOST_COMMON["colour"]
    if common not in blocked:
        picks.append(common)                                             # the most common wrong guess
    # Prefer the flat, contrasting set; then any other colour that still contrasts.
    preferred = contrasting_colours(truth, 8)
    rest = sorted((c for c in COLOUR_LAB if c not in preferred), key=lambda c: -delta_e2000(COLOUR_LAB[c], COLOUR_LAB[truth]))
    for cand in preferred + rest:
        if len(picks) == N_OPTIONS - 1:
            break
        if cand in blocked or cand in picks or cand == truth:
            continue
        if any(colours_confusable(cand, p) or delta_e2000(COLOUR_LAB[cand], COLOUR_LAB[p]) < 25 for p in picks):
            continue
        picks.append(cand)
    if len(picks) < N_OPTIONS - 1:
        return None
    return [truth.title()] + [p.title() for p in picks]


def _set_options(truth, values, common, exclude=()):
    """Truth + 3 other values of the same kind: the most common one first, then random."""
    others = [v for v in values if v != truth and v not in exclude]
    if len(others) < N_OPTIONS - 1:
        return None
    picks = [common] if common in others else []
    rest = [v for v in others if v not in picks]
    _rng.shuffle(rest)
    picks += rest[:N_OPTIONS - 1 - len(picks)]
    return [truth] + picks


def candidate_questions(found, lost, visual, semantic, private_attrs=None) -> list:
    """
    Every question this item can support, strongest (most hidden) first.
    Each: {attribute, truth, options (truth first, unshuffled), source, confidence}.
    """
    from matching import Facts
    category = found.get("questionCategory") or found.get("category") or "other"
    known = _known_to_claimant(lost, found)
    notes = Facts(found.get("notes") or "")
    out = []

    # 1. Secret details staff recorded as multiple choice (checked against the stored hash).
    for name, attr in (private_attrs or {}).items():
        if attr.get("choices") and not attr.get("leaked") and len(attr["choices"]) >= 3:
            out.append({"attribute": f"private:{name}", "truth": None, "options": list(attr["choices"]),
                        "source": "staff", "confidence": 1.0, "private": name, "verified": True})

    # 2. What was inside (depot notes / scan), not something the claimant wrote.
    pool = CONTENT_POOLS.get(category)
    contents = [c for c in notes.contents if pool and c in pool and c not in known]
    if contents:
        opts = _set_options(contents[0], list(pool), MOST_COMMON["content_item"], exclude=set(notes.contents))
        if opts:
            out.append({"attribute": "content_item", "truth": contents[0], "options": [o.title() for o in opts],
                        "source": "notes", "confidence": 0.9, "verified": True})

    # 3. Brand (notes / OCR / logo) from the item's own market, unless the claimant named it.
    found_brands = [b for b in notes.brands if category in BRAND_HOMES.get(b, ()) and b not in known
                    and not any(w in known for w in normalize_text(b).split())]
    market = BRANDS.get(category) or ()
    if found_brands and len(market) >= N_OPTIONS:
        truth = next((m for m in market if m.casefold() == found_brands[0]), None)
        if truth:
            opts = _set_options(truth, list(market), market[0])
            if opts:
                out.append({"attribute": "brand", "truth": truth, "options": opts, "source": "notes",
                            "confidence": 0.9, "verified": True})

    # Colours: measured on the found photos, else the depot's notes.
    scan = {c["name"]: float(c.get("share") or 0) for c in ((found.get("attributes") or {}).get("colours") or [])
            if isinstance(c, dict) and c.get("name") in COLOUR_SET}
    colours = dict(visual.get("colours") or {}) or scan or {c: 1.0 for c in notes.colours}
    ranked = [c for c, s in sorted(colours.items(), key=lambda kv: -kv[1]) if s >= 0.08]
    main = ranked[0] if ranked else None

    # 4. A second colour on the item (owners know their item's colours; others rarely do).
    second = next((c for c in ranked[1:] if colours[c] >= 0.15 and not colours_confusable(c, main)), None)
    if main and second and second not in known:
        opts = _colour_options(second, ranked)
        if opts:
            out.append({"attribute": "second_colour", "truth": second, "options": opts, "source": "photo",
                        "confidence": min(0.95, 0.5 + colours[second]), "main": main, "verified": False})

    # 5. A visible feature the item has, with features it clearly does NOT have as options.
    feats = semantic.get("features") or {}
    ranked_feats = [f for f, _p in sorted(feats.items(), key=lambda kv: -kv[1])]
    if ranked_feats and feats[ranked_feats[0]] >= FEATURE_PROPOSE and len(ranked_feats) >= N_OPTIONS \
            and normalize_text(ranked_feats[0]).split()[-1] not in known:
        # The least likely features are the distractors; staff skip the question if two apply.
        out.append({"attribute": "feature", "truth": ranked_feats[0],
                    "options": [ranked_feats[0]] + ranked_feats[-(N_OPTIONS - 1):], "source": "photo",
                    "confidence": feats[ranked_feats[0]], "verified": False})

    # 6. Material (as distinct groups, so nylon and polyester are never two options).
    mat = semantic.get("material")
    note_mat = next((g for g, words in MATERIALS.items() if set(words) & set(notes.materials)), None)
    material = note_mat or (mat[0] if mat else None)
    if material and not set(MATERIALS[material]) & known:
        opts = _set_options(material, list(MATERIALS), MOST_COMMON["material"])
        if opts:
            out.append({"attribute": "material", "truth": material, "options": opts,
                        "source": "notes" if note_mat else "photo", "confidence": 0.9 if note_mat else mat[1],
                        "verified": bool(note_mat)})

    # 7. Style of the item (a backpack, a trolley…), unless the claimant or the public text said it.
    style = semantic.get("style")
    if style and not set(normalize_text(style[0]).split()) & known:
        opts = _set_options(style[0], list(STYLES[category]), STYLES[category][0])
        if opts:
            out.append({"attribute": "style", "truth": style[0], "options": opts, "source": "photo",
                        "confidence": style[1], "verified": False})

    # 8. Surface pattern.
    pattern = semantic.get("pattern")
    if pattern and not set(normalize_text(pattern[0]).split()) & known - {"one", "design"}:
        opts = _set_options(pattern[0], list(PATTERNS), MOST_COMMON["pattern"])
        if opts:
            out.append({"attribute": "pattern", "truth": pattern[0], "options": opts, "source": "photo",
                        "confidence": pattern[1], "verified": False})

    # 9. The main colour, only if neither the claimant nor the public text gave it away.
    if main and main not in known:
        opts = _colour_options(main, ranked)
        if opts and option_flatness([o.lower() for o in opts]) <= 0.6:
            out.append({"attribute": "main_colour", "truth": main, "options": opts,
                        "source": "notes" if not visual.get("colours") and not scan else "photo",
                        "confidence": min(0.95, 0.5 + colours[main]),
                        "verified": not visual.get("colours") and not scan})
    return out


# ── wording (LLM, validated) ─────────────────────────────────────────────────
def _template(q, noun):
    if q.get("private"):
        from claims.attributes import question_text
        return question_text(q["private"], {"category": q.get("category"), "itemNoun": noun})
    return TEMPLATES[q["attribute"]].format(noun=noun, main=(q.get("main") or "").lower())


# An LLM-worded question must say what it is about ("What type of object was found?"
# for a contents question was a real, ambiguous output); else the template is used.
SUBJECT_WORDS = {"content_item": ("inside", "contain", "packed", "in your", "in the"),
                 "material": ("made", "material"), "pattern": ("pattern", "design", "look", "surface"),
                 "main_colour": ("colour", "color"), "second_colour": ("colour", "color"),
                 "brand": ("brand", "make"), "feature": ("has", "have", "feature", "part", "attached"),
                 "style": ("kind", "type", "style", "sort")}


def _valid_wording(text, options, attribute=None) -> bool:
    if not isinstance(text, str) or not 8 <= len(text) <= 140 or not text.strip().endswith("?"):
        return False
    low = text.lower()
    if any(o.lower() in low for o in options if len(o) > 2):                 # never names an option
        return False
    words = SUBJECT_WORDS.get((attribute or "").split(":")[0])
    return not words or any(w in low for w in words)


def llm_wording(questions, noun, client=None):
    """
    Ask the LLM to word each question naturally (and suggest distractors). Returns
    {index: {"text": ..., "distractors": [...]}}; anything invalid is dropped.
    """
    key = os.environ.get("GROQ_API_KEY")
    if client is None and not key:
        return {}
    model = (os.environ.get("GROQ_REASONING_MODEL") or "openai/gpt-oss-120b").strip()
    items = [{"index": i, "attribute": q["attribute"].replace("private:", ""), "correct_answer": q["options"][0],
              "current_options": q["options"]} for i, q in enumerate(questions) if not q.get("private")]
    if not items:
        return {}
    system = (
        "You write anti-fraud security questions for a bus lost-and-found counter. A person claims a "
        f"found {noun}; only its real owner should answer correctly. For each item you get the attribute and "
        "the CORRECT answer, which comes from the real item. Rules: (1) word one short, plain-English "
        "question per item, ending with '?'; the question must not contain the correct answer or any option; "
        "(2) optionally suggest exactly 3 wrong options of the SAME kind as the correct answer that are "
        "clearly different values, never a synonym or near-synonym of it (no 'navy' for 'blue', no "
        "'polyester' for 'nylon'), and one of the 3 must be the most commonly guessed wrong answer for that "
        "kind of question; (3) never change the correct answer. Reply with JSON only: "
        '{"questions": [{"index": 0, "question": "...", "distractors": ["...", "...", "..."]}]}')
    try:
        import groq
        client = client or groq.Groq(api_key=key, timeout=20)
        resp = client.chat.completions.create(
            model=model, temperature=0.2, max_tokens=1500,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(items)}],
            **({"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}))
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
    except Exception as e:                    # outage, timeout, bad JSON: the rules wording stands
        logger.warning("Security-question wording via %s failed: %s", model, e)
        return {}
    out = {}
    for row in data.get("questions") or []:
        if isinstance(row, dict) and isinstance(row.get("index"), int):
            out[row["index"]] = {"text": row.get("question"), "distractors": row.get("distractors")}
    return out


def _valid_distractors(q, suggested) -> list | None:
    """LLM distractors only if they are the same kind, distinct, not synonyms and not on the item."""
    if not isinstance(suggested, list) or len(suggested) != N_OPTIONS - 1:
        return None
    truth = q["options"][0]
    attr = q["attribute"]
    vals = [str(s).strip() for s in suggested]
    if len({v.casefold() for v in vals} | {truth.casefold()}) != N_OPTIONS:
        return None
    if attr in ("second_colour", "main_colour"):
        names = [v.lower() for v in vals]
        if not all(n in COLOUR_LAB for n in names):
            return None
        group = [truth.lower()] + names
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if colours_confusable(a, b) or delta_e2000(COLOUR_LAB[a], COLOUR_LAB[b]) < 25:
                    return None
        if MOST_COMMON["colour"] not in names and not colours_confusable(MOST_COMMON["colour"], truth.lower()):
            return None
        return [n.title() for n in names]
    allowed = {"material": list(MATERIALS), "pattern": list(PATTERNS)}.get(attr)
    if allowed is None:
        return None                                      # other kinds keep the rule-made options
    if not all(v in allowed for v in vals) or truth in vals:
        return None
    return vals


# ── building a question set ──────────────────────────────────────────────────
def build_question_set(found, lost, image_root, classifier=None, private_attrs=None, use_llm=True,
                       llm_client=None, max_questions=MAX_CANDIDATES):
    """
    Returns {"questions": [...stored form, answer index server-side...], "source", "insufficient",
    "available", "facts"}. Questions with "verified": False still need staff confirmation.
    """
    category = found.get("questionCategory") or found.get("category") or "other"
    noun = (found.get("itemNoun") or NOUNS.get(category, "item")).lower()
    rel = found.get("images") or ([found["image_path"]] if found.get("image_path") else [])
    paths = [os.path.join(image_root, p) for p in rel[:6] if p]
    paths = [p for p in paths if os.path.exists(p)]
    visual = {}
    per_view = [v for v in (visual_facts(p) for p in paths) if v]
    if per_view:
        merged = {}
        for v in per_view:
            for c, s in v["colours"].items():
                merged[c] = merged.get(c, 0.0) + s / len(per_view)
        visual = {**per_view[0], "colours": dict(sorted(merged.items(), key=lambda kv: -kv[1])),
                  "views": len(per_view)}
    semantic = semantic_facts(classifier, paths, category)
    cands = candidate_questions(found, lost, visual, semantic, private_attrs)
    chosen = cands[:max_questions]
    for q in chosen:
        q["category"] = category
    wording = llm_wording(chosen, noun, llm_client) if use_llm else {}
    source = "llm" if wording else "rules"
    stored = []
    for i, q in enumerate(chosen):
        w = wording.get(i) or {}
        text = w.get("text") if _valid_wording(w.get("text"), q["options"], q["attribute"]) else _template(q, noun)
        options = list(q["options"])
        if not q.get("private"):
            better = _valid_distractors(q, w.get("distractors"))
            if better:
                options = [options[0]] + better
        order = list(range(len(options)))
        _rng.shuffle(order)
        shuffled = [options[j] for j in order]
        entry = {"id": f"q{i + 1}", "attribute": q["attribute"], "text": text, "options": shuffled,
                 "source": q["source"], "verified": bool(q.get("verified"))}
        if q.get("private"):
            entry["private"] = q["private"]                         # checked against the stored hash
        else:
            entry["answerIndex"] = order.index(0)                   # server-side only
        stored.append(entry)
    insufficient = len(stored) < MIN_QUESTIONS
    facts = {"visual": {k: v for k, v in visual.items() if k != "colours"} | {"colours": visual.get("colours")},
             "semantic": {k: (v if k != "features" else {f: round(p, 2) for f, p in v.items()})
                          for k, v in semantic.items()}}
    return {"questions": stored, "source": source, "insufficient": insufficient,
            "available": len(stored), "facts": facts}


def needs_staff_check(questions) -> list:
    return [q for q in questions if not q.get("verified")]


def apply_staff_check(questions, picks):
    """
    Staff, holding the item, pick the correct option of every photo-derived question
    (or "skip"). Their pick becomes the answer. Returns (questions_to_ask, agreement)
    where agreement = (AI guesses staff agreed with, guesses checked). Options are
    re-shuffled so the claimant cannot learn anything from where staff tapped.
    """
    kept, agreed, checked = [], 0, 0
    for q in questions:
        q = dict(q)
        if not q.get("verified"):
            pick = picks.get(q["id"])
            if not isinstance(pick, int) or not 0 <= pick < len(q["options"]):
                continue                                            # skipped or missing: not asked
            checked += 1
            agreed += pick == q.get("answerIndex")
            q["answerIndex"], q["verified"], q["confirmedBy"] = pick, True, "staff"
        kept.append(q)
    final = []
    for i, q in enumerate(sorted(kept, key=lambda x: _PRIORITY.get(x["attribute"].split(":")[0], 9))[:MAX_QUESTIONS]):
        order = list(range(len(q["options"])))
        _rng.shuffle(order)
        q = {**q, "id": f"q{i + 1}", "options": [q["options"][j] for j in order]}
        if "answerIndex" in q:
            q["answerIndex"] = order.index(q["answerIndex"])
        final.append(q)
    return final, (agreed, checked)


# Which kinds of question to keep when there are more than MAX_QUESTIONS (most hidden first).
_PRIORITY = {"private": 0, "content_item": 1, "brand": 2, "second_colour": 3, "feature": 4, "material": 5,
             "style": 6, "pattern": 7, "main_colour": 8}


def public_view(questions) -> list:
    """What the browser may see: never the answer index, never which source it came from."""
    return [{"id": q["id"], "text": q["text"], "options": list(q["options"])} for q in questions]


def score_answers(questions, answers, private_attrs=None, answer_key=None, claims_config=None) -> tuple:
    """(correct, total). `answers` maps question id → option index chosen."""
    from claims.attributes import ATTRIBUTES
    from claims.scoring import answer_passes
    correct = 0
    for q in questions:
        idx = answers.get(q["id"])
        if not isinstance(idx, int) or not 0 <= idx < len(q["options"]):
            continue
        if q.get("private"):
            attr = (private_attrs or {}).get(q["private"])
            if attr and answer_key and answer_passes(answer_key, attr, q["options"][idx], claims_config,
                                                     generic=ATTRIBUTES.get(q["private"], {}).get("generic", ())):
                correct += 1
        elif idx == q.get("answerIndex"):
            correct += 1
    return correct, len(questions)


def outcome(correct, total) -> str:
    if total and correct >= math.ceil(PASS_SHARE * total - 1e-9):
        return "pass"
    if total and correct >= math.ceil(ESCALATE_SHARE * total - 1e-9):
        return "escalate"
    return "fail"


def guess_pass_probability(total, n_options=N_OPTIONS) -> float:
    """Chance a blind guesser reaches the pass mark on one attempt (for the docs and tests)."""
    need = math.ceil(PASS_SHARE * total - 1e-9)
    p = 1 / n_options
    return sum(math.comb(total, k) * p ** k * (1 - p) ** (total - k) for k in range(need, total + 1))


__all__ = ["build_question_set", "public_view", "score_answers", "outcome", "visual_facts", "semantic_facts",
           "needs_staff_check", "apply_staff_check",
           "candidate_questions", "guess_pass_probability", "QUESTION_TTL", "MAX_FAILED_ATTEMPTS",
           "MIN_QUESTIONS"]
