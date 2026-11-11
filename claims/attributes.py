"""
Private attributes of found items and adaptive question selection.

Staff record attributes ONLY while logging the item. They are encoded straight
away (see normalize.py) and the raw values are discarded, so no screen, API or
database field can reveal them later. An attribute whose words also appear in
the item's public description (which is sent in the Feature 2 notification) is
flagged `leaked` and is never asked.

Phase 2: an attribute may carry `choices` — the option labels of a multiple-
choice question (the true value plus distractors, see claims/options.py). The
labels are shown to claimants; which one is right is still only knowable
through the hashed entries.

Question bank v2 (Nov 2026): every question is asked only of the kinds of item
it makes sense for (no zip on a phone, no brand of a document, nothing "inside"
an umbrella), and is phrased with the item's own noun ("the backpack", "the
phone's case"), following the per-category control questions real lost-property
systems use (iLost) and OWASP's rule that a question must be applicable to the
item. An item whose category the camera was not sure about gets only the
questions that suit any item (`questionCategory` = "other").
"""
import secrets

from .normalize import colour_tokens, encode_tokens, tokens

CATEGORIES = ("bag", "wallet", "phone", "laptop", "spectacles", "umbrella", "documents", "clothing", "other")
ALL = frozenset(CATEGORIES)

# kind, weight, the staff form label, and one question per category it applies
# to ({noun} is the item's own word: "backpack", "phone", "spectacles").
# Weights follow how SECRET the answer is: facts hidden inside or on the screen
# weigh 2, facts a co-passenger could see from outside (zip colour, a brand
# logo, a sticker) weigh 1 or less, so seeing the item is never enough to pass.
ATTRIBUTES = {
    "zipper_tag_colour": {
        "kind": "colour", "weight": 1.0, "label": "Zip pull / tag colour",
        "questions": {"bag": "What colour is the zip pull or the tag on the {noun}?"}},
    "content_item": {
        "kind": "text", "weight": 2.0, "label": "One item inside",
        "questions": {"bag": "Name one thing that was inside the {noun}.",
                      "wallet": "Name one thing that was inside the {noun}.",
                      "documents": "What kind of papers were in the {noun}?",
                      "clothing": "Name one thing that was in the {noun}'s pockets."}},
    "brand": {
        "kind": "text", "weight": 0.75, "label": "Brand / make",
        "questions": {"bag": "Which brand is the {noun}?", "wallet": "Which brand is the {noun}?",
                      "phone": "Which brand is the phone?", "laptop": "Which brand is the {noun}?",
                      "clothing": "Which brand label is on the {noun}?"}},
    "distinguishing_mark": {
        "kind": "text", "weight": 1.0, "label": "Sticker, scratch or mark",
        "questions": {"bag": "Describe a sticker, keychain, patch or mark on the {noun}.",
                      "wallet": "Describe a mark, initials or wear on the {noun}.",
                      "phone": "Describe a sticker, crack or mark on the phone or its case.",
                      "laptop": "Describe a sticker or mark on the {noun}'s lid.",
                      "spectacles": "Describe a mark or repair on the spectacles or their case.",
                      "umbrella": "Describe a mark, tear or tag on the umbrella.",
                      "documents": "Describe the folder or cover the papers were in.",
                      "clothing": "Describe a stain, tear, patch or name label on the {noun}.",
                      "other": "Describe a sticker, mark or name on the {noun}."}},
    "lock_screen": {
        "kind": "text", "weight": 2.0, "label": "Lock-screen wallpaper",
        "questions": {"phone": "What is on the phone's lock-screen wallpaper?",
                      "laptop": "What is on the {noun}'s login-screen wallpaper?"}},
    "case_colour": {
        "kind": "colour", "weight": 1.0, "label": "Case / sleeve colour",
        "questions": {"phone": "What colour is the phone's case or cover?",
                      "laptop": "What colour is the {noun}'s sleeve or cover?",
                      "spectacles": "What colour is the spectacles case?"}},
    "handle_colour": {
        "kind": "colour", "weight": 1.0, "label": "Handle colour",
        "questions": {"umbrella": "What colour is the umbrella's handle?"}},
    "tag_text": {
        "kind": "text", "weight": 2.0, "label": "Name or text on a tag",
        "questions": {"bag": "What name or words are written on the {noun}'s tag?",
                      "documents": "What name is written on the papers?",
                      "clothing": "What name or words are on the {noun}'s label?",
                      "other": "What name or words are written on the {noun}?"}},
}
# Words the QUESTION itself suggests ("Describe a sticker…" → "sticker"). They
# carry no knowledge, so they never count as a matching word on either side.
GENERIC_WORDS = {
    "distinguishing_mark": frozenset({"sticker", "keychain", "mark", "marks", "tag", "patch", "logo", "name",
                                      "label", "small", "big", "one", "stain", "tear", "torn", "scratch", "crack",
                                      "initials", "written", "wear"}),
    "lock_screen": frozenset({"photo", "picture", "pic", "image", "wallpaper", "screen", "lock", "background"}),
    "tag_text": frozenset({"name", "tag", "written", "label", "words"}),
}
for _name, _spec in ATTRIBUTES.items():
    _spec["generic"] = GENERIC_WORDS.get(_name, frozenset())
for _spec in ATTRIBUTES.values():
    _spec["categories"] = frozenset(_spec["questions"])
    _spec["question"] = _spec["questions"].get("other") or "Describe it."          # legacy field

# The word for the item when nothing more specific is known.
DEFAULT_NOUNS = {"bag": "bag", "wallet": "wallet", "phone": "phone", "laptop": "laptop",
                 "spectacles": "spectacles", "umbrella": "umbrella", "documents": "folder",
                 "clothing": "clothing", "other": "item"}
MAX_CHOICES = 4
MAX_CHOICE_CHARS = 40

# Facts only the owner can know: not visible to a co-passenger who saw the item.
HIDDEN = frozenset({"content_item", "lock_screen", "tag_text"})
MIN_QUESTIONS = 2
ROUND_QUESTIONS = 3
MAX_RAW_CHARS = 80
_rng = secrets.SystemRandom()


def applicable(category: str):
    return [k for k, a in ATTRIBUTES.items() if category in a["categories"]]


def encode_private_attributes(raw: dict, category: str, public_description: str, key: bytes,
                              fuzzy_min: int, choices=None) -> dict:
    """
    Raw staff input → stored hashed form. Unknown keys and inapplicable attributes
    are dropped. `choices` maps an attribute to its multiple-choice option labels
    (which must include the true value); options are kept only when valid.
    """
    choices = choices or {}
    category = category if category in CATEGORIES else "other"
    public = set(tokens(public_description))
    encoded = {}
    for name in applicable(category):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:MAX_RAW_CHARS]
        spec = ATTRIBUTES[name]
        toks = (colour_tokens(value) or tokens(value)) if spec["kind"] == "colour" else tokens(value)
        if not toks:
            continue
        encoded[name] = {
            "kind": spec["kind"], "weight": spec["weight"],
            "entries": encode_tokens(key, toks, fuzzy_min),
            "leaked": bool(public & set(toks)),
        }
        opts = _clean_choices(choices.get(name), value)
        if opts:
            encoded[name]["choices"] = opts
    return encoded


def _clean_choices(options, true_value):
    """Keep a choice list only if it is 3-4 distinct short labels that include the true value."""
    if not isinstance(options, (list, tuple)):
        return None
    seen, out = set(), []
    for opt in options:
        if isinstance(opt, str) and opt.strip() and len(opt.strip()) <= MAX_CHOICE_CHARS:
            label = opt.strip()
            if label.casefold() not in seen:
                seen.add(label.casefold())
                out.append(label)
    if not 3 <= len(out) <= MAX_CHOICES or true_value.strip().casefold() not in seen:
        return None
    _rng.shuffle(out)
    return out


def question_category(item) -> str:
    """The category questions are asked for: "other" when the camera was unsure."""
    cat = (item or {}).get("questionCategory") or (item or {}).get("category") or "other"
    return cat if cat in ALL else "other"


def askable(item) -> list:
    category = question_category(item)
    attrs = item.get("private_attributes") or {}
    return [k for k in applicable(category) if k in attrs and not attrs[k].get("leaked")]


def select_questions(item, previous=(), asked=()):
    """
    Choose this round's questions (2-3) plus one reserve for a borderline score.
    Varies across rounds and claims (CSPRNG shuffle), preferring questions not
    asked in the previous round. A multiple-choice question already asked in
    this claim (`asked`) is never asked again: its options are fixed, so a
    second try would let an impostor simply pick another option. Returns
    (round_ids, reserve_id_or_None), or ([], None) when too few are left.
    """
    attrs = item.get("private_attributes") or {}
    pool = [q for q in askable(item) if not (q in asked and attrs[q].get("choices"))]
    if len(pool) < MIN_QUESTIONS:
        return [], None
    _rng.shuffle(pool)
    ordered = [q for q in pool if q not in previous] + [q for q in pool if q in previous]
    # Someone who sat next to the owner saw the outside of the item (colours, brand
    # logo, stickers). Every round therefore leads with one HIDDEN fact when the item
    # has one: what was inside, the lock screen, the name on the tag.
    hidden = next((q for q in ordered if q in HIDDEN), None)
    if hidden:
        ordered.remove(hidden)
        ordered.insert(0, hidden)
    # 2 askable → 2 questions, no reserve; 3 → 2 + reserve; 4+ → 3 + reserve.
    count = ROUND_QUESTIONS if len(ordered) > ROUND_QUESTIONS else MIN_QUESTIONS
    ordered = _mix_formats(ordered, count, item.get("private_attributes") or {})
    if len(ordered) < MIN_QUESTIONS:
        return [], None
    rest = ordered[count:]
    # The follow-up for a borderline round: something only the owner knows, typed
    # rather than picked (a picked option can be guessed), in that order of preference.
    def rank(q):
        return (q not in HIDDEN or bool(attrs[q].get("choices")), q not in HIDDEN, bool(attrs[q].get("choices")))
    reserve = min(rest, key=rank) if rest else None
    return ordered[:count], reserve


def _mix_formats(ordered, count, attrs):
    """
    Every round must contain at least one free-text question: a round of 3-option
    multiple choice alone could be passed by guessing (1 in 9 for two questions).
    Rounds take at most two choice questions, the rest free text, and the reserve
    (the borderline follow-up) is free text whenever one is left. Returns [] when
    the item has no free-text question at all — the claim then goes to staff.
    """
    choice = [q for q in ordered if attrs.get(q, {}).get("choices")]
    text = [q for q in ordered if q not in choice]
    if not choice:
        return ordered
    if not text:
        return []
    head = choice[:min(2, count - 1)]
    head += text[:count - len(head)]
    head += [q for q in choice if q not in head][:count - len(head)]
    rest_text = [q for q in text if q not in head]
    rest_choice = [q for q in choice if q not in head]
    return head + rest_text + rest_choice


def item_noun(item) -> str:
    """'backpack', 'phone', 'spectacles'… the item's own word for question text."""
    item = item or {}
    noun = item.get("itemNoun")
    if isinstance(noun, str) and 2 <= len(noun) <= 24 and noun.replace(" ", "").isalpha():
        return noun.lower()
    return DEFAULT_NOUNS.get(question_category(item), "item")


def question_text(qid: str, item=None) -> str:
    spec = ATTRIBUTES[qid]
    cat = question_category(item)
    template = spec["questions"].get(cat) or spec["questions"].get("other") or next(iter(spec["questions"].values()))
    return template.format(noun=item_noun(item))


def question_view(qid: str, attrs=None, item=None) -> dict:
    view = {"id": qid, "text": question_text(qid, item)}
    opts = ((attrs or {}).get(qid) or {}).get("choices")
    if opts:
        view["options"] = list(opts)
    return view
