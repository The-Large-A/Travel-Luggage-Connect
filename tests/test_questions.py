"""
Ownership questions (question bank v2): do they make sense for the item, are
the options contrasting for the owner and flat for an impostor, and — through
the REAL hashing, question selection and scoring code — how often does a
genuine owner pass versus four kinds of impostor?

The simulation follows the knowledge-based-authentication literature: owners
recognise better than they recall, half-remember shades (Hang et al., CHI
2015; Bae et al. 2015 on colour category bias), and impostors either guess
blindly, guess the most popular answer, use the public description, or were
on the same bus and saw the outside of the item.
"""
import random

import pytest

from claims.attributes import (ATTRIBUTES, CATEGORIES, applicable, encode_private_attributes, item_noun,
                               question_text, question_view, select_questions)
from claims.config import ClaimsConfig
from claims.options import CONTENT_PRIOR, brand_options, build_choices, colour_options, content_options
from claims.attributes import HIDDEN
from claims.scoring import VERIFIED, answer_passes, round_verdict
from item_intake.questions import build_answers, question_category
from vocabulary import (BRANDS, COLOUR_LAB, COLOUR_PRIOR, CONTENT_POOLS, colours_confusable, delta_e2000,
                        option_flatness, similar_colours)

KEY = b"q" * 32
CFG = ClaimsConfig()
KINDS = {k: a["kind"] for k, a in ATTRIBUTES.items()}


# ── 1. every question suits the item it is asked about ────────────────────────
FORBIDDEN = {                                  # (attribute, category) pairs that make no sense
    ("zipper_tag_colour", c) for c in ("wallet", "phone", "laptop", "spectacles", "umbrella", "documents",
                                       "clothing", "other")} | {
    ("content_item", c) for c in ("phone", "laptop", "spectacles", "umbrella", "other")} | {
    ("brand", c) for c in ("documents", "umbrella", "spectacles", "other")} | {
    ("lock_screen", c) for c in CATEGORIES if c not in ("phone", "laptop")} | {
    ("case_colour", "umbrella"), ("case_colour", "bag"), ("handle_colour", "bag")}


@pytest.mark.parametrize("category", CATEGORIES)
def test_no_question_is_asked_of_an_item_it_does_not_fit(category):
    for attr in applicable(category):
        assert (attr, category) not in FORBIDDEN, f"{attr} asked of a {category}"
    assert len(applicable(category)) >= 2                       # always enough to form a round


@pytest.mark.parametrize("category", CATEGORIES)
def test_question_text_names_the_item(category):
    item = {"category": category, "itemNoun": None}
    for attr in applicable(category):
        text = question_text(attr, item)
        assert "{" not in text and text[-1] in "?." and len(text) < 90
        assert " it?" not in text and " it." not in text        # never a vague "it"
        noun = item_noun(item)
        words = {noun, category, "papers"} if category == "documents" else {noun, category}
        assert any(w in text for w in words), text


def test_question_uses_the_scanned_noun_and_rejects_junk_nouns():
    assert "backpack" in question_text("brand", {"category": "bag", "itemNoun": "backpack"})
    for junk in ("<b>x</b>", "a" * 40, "x", "bag1"):
        assert question_text("brand", {"category": "bag", "itemNoun": junk}) == "Which brand is the bag?"


def test_an_unsure_category_is_questioned_as_other():
    assert question_category({"category": "bag", "categoryConfidence": 0.95, "categoryMargin": 0.6}) == "bag"
    for conf, margin in ((0.79, 0.5), (0.95, 0.19), (0.5, 0.4), (0.0, 0.0)):
        assert question_category({"category": "bag", "categoryConfidence": conf, "categoryMargin": margin}) == "other"
    item = {"category": "phone", "questionCategory": "other", "private_attributes": {}}
    assert "lock-screen" not in question_text("distinguishing_mark", item)


# ── 2. options: contrasting, flat, plausible, stable ───────────────────────────
@pytest.mark.parametrize("colour", sorted(COLOUR_LAB))
def test_colour_options_are_contrasting_and_flat(colour):
    opts = colour_options(colour)
    if colour == "black":
        assert opts is None                                     # a third of all bags: free text instead
        return
    names = [o.lower() for o in opts]
    assert names[0] == colour and len(set(names)) == 4
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not colours_confusable(a, b), (a, b)
            assert delta_e2000(COLOUR_LAB[a], COLOUR_LAB[b]) >= 25, (a, b)
    assert option_flatness(names) <= 0.5


def test_colour_answers_in_any_language_become_one_canonical_option():
    assert colour_options("மஞ்சள்")[0] == "Yellow" and colour_options("sivappu")[0] == "Red"
    assert colour_options("red and black") is None and colour_options("") is None and colour_options("teal-ish") is None


@pytest.mark.parametrize("category", sorted(CONTENT_POOLS))
def test_content_options_fit_the_item_and_skip_what_was_seen_inside(category):
    pool = CONTENT_POOLS[category]
    for true in pool:
        seen = [p for p in pool if p != true][:1]
        res = content_options(true, category, exclude=seen)
        if res is None:
            continue
        label, opts = res
        names = [o.lower() for o in opts]
        assert label.lower() == true and names[0] == true and len(set(names)) == len(names) >= 3
        assert set(names) <= set(pool) and not set(names) & set(seen)
    assert content_options("umbrella", "wallet") is None        # not something a wallet holds
    assert content_options("tiffin", "phone") is None           # phones have no contents


def test_brand_options_stay_in_the_brands_market():
    for category, names in BRANDS.items():
        for brand in names:
            opts = brand_options(brand, category)
            if opts is None:
                continue
            assert opts[0].casefold() == brand.casefold() and len(set(o.casefold() for o in opts)) == len(opts)
            home = [c for c in BRANDS if any(o in BRANDS[c] for o in opts[1:])]
            assert home, (brand, opts)
    assert brand_options("NoSuchBrand", "bag") is None


def test_options_are_stored_once_and_shown_identically_to_every_claimant():
    raw, choices = build_choices({"zipper_tag_colour": "red", "content_item": "tiffin"}, "bag", KINDS)
    attrs = encode_private_attributes(raw, "bag", "Black bag", KEY, 5, choices=choices)
    views = {tuple(question_view("zipper_tag_colour", attrs, {"category": "bag"})["options"]) for _ in range(20)}
    assert len(views) == 1


def test_the_true_option_is_never_marked_in_storage():
    raw, choices = build_choices({"zipper_tag_colour": "pink"}, "bag", KINDS)
    attrs = encode_private_attributes(raw, "bag", "Black bag", KEY, 5, choices=choices)
    stored = attrs["zipper_tag_colour"]
    assert set(stored) == {"kind", "weight", "entries", "leaked", "choices"}
    assert "pink" not in repr(stored["entries"])


def test_the_dominant_colour_is_never_asked():
    raw, choices = build_choices({"zipper_tag_colour": "red"}, "bag", KINDS)
    attrs = encode_private_attributes(raw, "bag", "Red backpack", KEY, 5, choices=choices)
    assert attrs["zipper_tag_colour"]["leaked"]                 # in the public description → not asked


# ── 3. genuine owners vs impostors, through the real scoring ───────────────────
NAMES = ["Arun", "Priya", "Meena", "Ravi", "Karthik", "Lakshmi", "Anu", "Senthil", "Divya", "Bala"]
MARKS = ["CSK sticker", "Ganesha keychain", "red ribbon", "torn pocket", "name embroidered", "blue patch"]
WALLPAPERS = ["temple photo", "my daughter", "beach sunset", "Vijay poster", "cricket team"]
PREFERRED = {"bag": "Wildcraft", "wallet": "Woodland", "phone": "Samsung", "laptop": "Dell", "clothing": "Raymond"}


def _weighted(rng, prior, pool):
    weights = [prior.get(p, 0.03) for p in pool]
    return rng.choices(pool, weights=weights, k=1)[0]


def _make_item(rng, category, dominant):
    """A found item with realistic secret attributes, as staff or the scan would record them."""
    truth = {}
    for attr in applicable(category):
        if ATTRIBUTES[attr]["kind"] == "colour":
            truth[attr] = _weighted(rng, COLOUR_PRIOR, [c for c in COLOUR_PRIOR if c != dominant])
        elif attr == "content_item":
            truth[attr] = _weighted(rng, CONTENT_PRIOR, list(CONTENT_POOLS[category]))
        elif attr == "brand":
            truth[attr] = rng.choice(BRANDS[category])
        elif attr == "tag_text":
            truth[attr] = rng.choice(NAMES)
        elif attr == "lock_screen":
            truth[attr] = rng.choice(WALLPAPERS)
        else:
            truth[attr] = rng.choice(MARKS)
    return truth


def _options_v1(truth):
    """The v1 choices: nearest colours (2), random contents, as stored before the redesign."""
    choices = {}
    for attr, value in truth.items():
        if ATTRIBUTES[attr]["kind"] == "colour":
            choices[attr] = [value] + similar_colours(value, 2)
    return truth, choices


def _closest_option(recalled, options):
    """An owner picks the option that best fits what they remember."""
    return min(options, key=lambda o: (0 if o.lower() == recalled else 1 if colours_confusable(o.lower(), recalled)
                                       else 2, delta_e2000(COLOUR_LAB.get(o.lower(), COLOUR_LAB["grey"]),
                                                           COLOUR_LAB[recalled])))


def _owner_answer(rng, attr, true, opts):
    kind = ATTRIBUTES[attr]["kind"]
    if kind == "colour":
        recalled = true
        if rng.random() < 0.25:                                  # half-remembered shade
            recalled = rng.choice([c for c in COLOUR_LAB if c != true and colours_confusable(c, true)])
        return _closest_option(recalled, opts) if opts else recalled
    recall = {"content_item": 0.9, "brand": 0.75, "lock_screen": 0.9, "tag_text": 0.8,
              "distinguishing_mark": 0.8}[attr]
    if rng.random() > recall:
        return rng.choice(opts) if opts else "i don't remember"
    if opts:
        return next(o for o in opts if o.casefold() == true.casefold())
    if rng.random() < 0.3 and len(true) > 5:                     # a typo
        i = rng.randrange(1, len(true) - 1)
        return true[:i] + true[i + 1:]
    return true


def _impostor_answer(rng, kind_of_impostor, attr, opts, dominant, truth):
    kind = ATTRIBUTES[attr]["kind"]
    if kind_of_impostor == "co_traveller":                       # saw the outside of the item
        visible = {"zipper_tag_colour": 0.5, "brand": 0.5, "distinguishing_mark": 0.3, "handle_colour": 0.5,
                   "case_colour": 0.6}
        if rng.random() < visible.get(attr, 0.0):
            true = truth[attr]
            return next((o for o in opts if o.casefold() == true.casefold()), true) if opts else true
    if opts:
        if kind_of_impostor == "random":
            return rng.choice(opts)
        prior = COLOUR_PRIOR if kind == "colour" else CONTENT_PRIOR
        if kind_of_impostor == "public_info" and kind == "colour":
            return _closest_option(dominant, opts)
        return max(opts, key=lambda o: prior.get(o.lower(), 0.0))   # the most popular option
    guesses = {"colour": "black", "content_item": "clothes", "brand": "Wildcraft", "tag_text": "Kumar",
               "lock_screen": "god photo", "distinguishing_mark": "sticker"}
    return guesses["colour" if kind == "colour" else attr]


def _protocol(rng, who, raw, attrs, item, dominant):
    """
    The claim protocol of claims/service.py: up to CFG.max_rounds rounds of fresh
    questions; a borderline round gets one strict follow-up; a second borderline
    goes to staff. Returns "verified", "staff" or "locked".
    """
    previous, asked = (), set()
    for _round in range(CFG.max_rounds):
        chosen, reserve = select_questions(item, previous, asked)
        if not chosen:
            return "staff"
        outcomes, strict = {}, False
        pending = list(chosen)
        while True:
            for q in pending:
                opts = attrs[q].get("choices") or []
                true = raw[q]
                ans = (_owner_answer(rng, q, true.lower() if ATTRIBUTES[q]["kind"] == "colour" else true, opts)
                       if who == "owner" else _impostor_answer(rng, who, q, opts, dominant, raw))
                on_menu = not opts or ans.strip() in opts
                outcomes[q] = on_menu and answer_passes(KEY, attrs[q], ans, CFG, strict,
                                                        generic=ATTRIBUTES[q]["generic"])
            asked |= set(pending)
            _score, verdict = round_verdict(outcomes, {q: ATTRIBUTES[q]["weight"] for q in outcomes}, HIDDEN, CFG)
            if verdict == "borderline" and not strict and reserve:
                pending, strict, reserve = [reserve], True, None
                continue
            break
        if verdict == VERIFIED:
            return "verified"
        if verdict == "borderline":
            return "staff"
        previous = tuple(chosen)
    return "locked"


def _simulate(builder, who, n=400, seed=11, hidden_only=False):
    rng = random.Random(seed)
    results = []
    for _ in range(n):
        category = rng.choice([c for c in CATEGORIES if c != "other"])
        dominant = _weighted(rng, COLOUR_PRIOR, list(COLOUR_PRIOR))
        truth = _make_item(rng, category, dominant)
        raw, choices = builder(truth, category)
        attrs = encode_private_attributes(raw, category, f"{dominant.title()} {category}", KEY, 5, choices=choices)
        item = {"category": category, "private_attributes": attrs}
        if not select_questions(item)[0] or (hidden_only and not HIDDEN & set(raw)):
            continue
        results.append(_protocol(rng, who, raw, attrs, item, dominant))
    return {k: results.count(k) / len(results) for k in ("verified", "staff", "locked")}


def _v2(truth, category):
    return build_choices(truth, category, KINDS)


def _v1(truth, category):
    return _options_v1(truth)


@pytest.fixture(scope="module")
def rates():
    return {who: _simulate(_v2, who) for who in ("owner", "random", "popularity", "public_info", "co_traveller")}


def test_genuine_owners_are_never_locked_out(rates):
    owner = rates["owner"]
    assert owner["locked"] <= 0.01 and owner["verified"] + owner["staff"] >= 0.99, rates


def test_owners_of_items_with_a_hidden_fact_pass_without_staff():
    """Umbrellas and spectacles have nothing hidden to ask about, so they always get a staff ID check."""
    owner = _simulate(_v2, "owner", hidden_only=True)
    assert owner["verified"] >= 0.85, owner


@pytest.mark.parametrize("impostor,limit", [("random", 0.03), ("popularity", 0.05), ("public_info", 0.05),
                                            ("co_traveller", 0.10)])
def test_impostors_rarely_pass(rates, impostor, limit):
    assert rates[impostor]["verified"] <= limit, rates


def test_contrasting_colours_help_owners_without_helping_impostors():
    """Same owners, same items: only the colour options differ (v1 nearest shades vs v2 contrasting)."""
    def colour_only(builder, who):
        rng = random.Random(5)
        hits = total = 0
        for _ in range(2000):
            true = _weighted(rng, COLOUR_PRIOR, [c for c in COLOUR_PRIOR if c != "black"])
            _raw, choices = builder({"zipper_tag_colour": true}, "bag")
            opts = choices["zipper_tag_colour"]
            if who == "owner":
                ans = _owner_answer(rng, "zipper_tag_colour", true, opts)
            else:
                ans = rng.choice(opts)
            hits += ans.lower() == true
            total += 1
        return hits / total
    owner_v1, owner_v2 = colour_only(_v1, "owner"), colour_only(_v2, "owner")
    guess_v1, guess_v2 = colour_only(_v1, "random"), colour_only(_v2, "random")
    assert owner_v2 >= 0.95 and owner_v2 > owner_v1 + 0.1, (owner_v1, owner_v2)   # v1 confused real owners
    assert guess_v2 <= guess_v1                                    # 1 in 4 now, 1 in 3 before


# ── 4. the rules that keep co-passengers and guessers out ──────────────────────
def _mark(value):
    return encode_private_attributes({"distinguishing_mark": value}, "bag", "Black bag", KEY, 5)["distinguishing_mark"]


def test_words_the_question_suggests_are_not_an_answer():
    generic = ATTRIBUTES["distinguishing_mark"]["generic"]
    mark = _mark("CSK sticker")
    assert not answer_passes(KEY, mark, "sticker", CFG, generic=generic)          # parroting the question
    assert not answer_passes(KEY, mark, "a small sticker", CFG, generic=generic)
    assert answer_passes(KEY, mark, "csk sticker", CFG, generic=generic)
    assert answer_passes(KEY, mark, "CSK", CFG, generic=generic)
    assert answer_passes(KEY, mark, "sticker", CFG)                                # v1 behaviour, for contrast
    plain = _mark("keychain")                                                      # the scan only knew "keychain"
    assert answer_passes(KEY, plain, "keychain", CFG, generic=generic)             # all-generic: kept as is


def test_a_round_verifies_only_when_a_hidden_fact_was_right():
    weights = {q: ATTRIBUTES[q]["weight"] for q in ATTRIBUTES}
    _s, v = round_verdict({"content_item": True, "brand": True, "zipper_tag_colour": True}, weights, HIDDEN, CFG)
    assert v == VERIFIED
    _s, v = round_verdict({"content_item": False, "brand": True, "zipper_tag_colour": True}, weights, HIDDEN, CFG)
    assert v != VERIFIED                                                           # saw it, did not own it
    _s, v = round_verdict({"handle_colour": True, "distinguishing_mark": True}, weights, HIDDEN, CFG)
    assert v == "borderline"                                       # an umbrella: nothing hidden → staff check


def test_multiple_choice_is_never_asked_twice_in_a_claim():
    raw, choices = build_choices({"zipper_tag_colour": "red", "content_item": "tiffin", "brand": "Wildcraft",
                                  "tag_text": "ANU", "distinguishing_mark": "CSK sticker"}, "bag", KINDS)
    attrs = encode_private_attributes(raw, "bag", "Black bag", KEY, 5, choices=choices)
    item = {"category": "bag", "private_attributes": attrs}
    for _ in range(30):
        first, _r = select_questions(item)
        again, _r = select_questions(item, previous=first, asked=set(first))
        repeated_choice = [q for q in again if q in first and attrs[q].get("choices")]
        assert not repeated_choice


def test_hidden_facts_lead_every_round():
    raw, choices = build_choices({"zipper_tag_colour": "red", "content_item": "tiffin", "brand": "Wildcraft",
                                  "distinguishing_mark": "CSK sticker"}, "bag", KINDS)
    attrs = encode_private_attributes(raw, "bag", "Black bag", KEY, 5, choices=choices)
    for _ in range(30):
        chosen, _r = select_questions({"category": "bag", "private_attributes": attrs})
        assert "content_item" in chosen
