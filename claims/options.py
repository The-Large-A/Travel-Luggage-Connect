"""
Multiple-choice options for ownership questions (question bank v2).

Three properties, each from the research behind the redesign:

  * CONTRASTING for the owner — every option is clearly different from the
    true value and from each other. For colours: no two options a person could
    call by either name (charcoal black/grey, navy black/blue, maroon red/brown)
    and CIEDE2000 >= 25 between all of them. The v1 options were the NEAREST
    colours (red / pink / orange), which genuine owners could not tell apart in
    a bus at night, while impostors lost nothing.
  * FLAT for the impostor — options about as common as the true value, so
    "pick the most popular" does not win (the honeyword flatness idea, Juels &
    Rivest). When no flat set exists (black is a third of all bags) the
    question is asked as free text instead.
  * PLAUSIBLE for the item — "one thing inside a wallet" never offers an
    umbrella; brands come from the item's own market; nothing the depot saw
    inside the item is offered as a wrong answer.

Options are generated once, when the item is logged, and stored (shuffled)
with the hashed answer, so every claimant sees the same set: an impostor
cannot intersect several claims to find the constant option.
"""
import secrets

from vocabulary import (BRAND_HOMES, BRANDS, CONTENT_POOLS, contrasting_colours,
                        option_flatness)

from .normalize import colour_tokens, tokens

N_OPTIONS = 4
MAX_FLATNESS = 0.6                 # above this a blind guess is too good: ask as free text
_rng = secrets.SystemRandom()

# How often each thing turns up inside a lost item (rough shares; only the
# ORDER matters, for picking distractors about as common as the true answer).
CONTENT_PRIOR = {"clothes": 0.16, "money": 0.12, "card": 0.1, "bottle": 0.1, "tiffin": 0.09, "charger": 0.09,
                 "book": 0.07, "keys": 0.06, "photo": 0.05, "ticket": 0.05, "bill": 0.04, "towel": 0.04,
                 "medicine": 0.04, "earphones": 0.04, "spectacles": 0.03, "umbrella": 0.03, "wallet": 0.03,
                 "powerbank": 0.02, "certificate": 0.04, "passport": 0.01}


def colour_options(value):
    """[true, 3 contrasting colours] as labels, or None (free text)."""
    named = colour_tokens(value or "")
    if len(named) != 1:
        return None                                   # "red and black": not a single-answer choice
    true = named[0]
    opts = [true] + contrasting_colours(true, N_OPTIONS - 1)
    if len(opts) < 3 or option_flatness(opts) > MAX_FLATNESS:
        return None
    return [o.title() for o in opts]


def content_options(value, category, exclude=()):
    """
    (true_label, options) for "one thing inside", or None. The true value must
    be one canonical thing from the category's pool; distractors are other
    things that fit that kind of item, about as common, never seen inside it.
    """
    pool = CONTENT_POOLS.get(category)
    toks = [t for t in tokens(value or "") if t in (pool or ())]
    if not pool or len(toks) != 1:
        return None
    true = toks[0]
    excluded = {true} | {t for e in exclude for t in tokens(e)}
    candidates = [c for c in pool if c not in excluded]
    if len(candidates) < 2:
        return None
    target = CONTENT_PRIOR.get(true, 0.05)
    candidates.sort(key=lambda c: abs(CONTENT_PRIOR.get(c, 0.05) - target))
    near = candidates[:5]                             # about as common: pick unpredictably among them
    _rng.shuffle(near)
    picked = near[:N_OPTIONS - 1]
    return true.title(), [true.title()] + [c.title() for c in picked]


def brand_options(value, category):
    """[true, 3 brands from the same market], or None for an unknown brand."""
    true = next((b for b in BRAND_HOMES if b == (value or "").strip().casefold()), None)
    if true is None:
        return None
    homes = BRAND_HOMES[true]
    market = category if category in homes else next((c for c in BRANDS if c in homes and len(BRANDS[c]) >= 4),
                                                      None)
    pool = [b for b in BRANDS.get(market, ()) if b.casefold() != true]
    if len(pool) < 2:
        return None
    _rng.shuffle(pool)
    label = next((b for names in BRANDS.values() for b in names if b.casefold() == true), value.strip())
    return [label] + pool[:N_OPTIONS - 1]


def build_choices(raw: dict, category: str, kinds: dict, exclude_contents=(), colours_only=False):
    """
    (raw, choices): option lists for the attributes that suit multiple choice.
    A content answer is canonicalised to the option label it will be checked
    against ("steel tiffin box" → "Tiffin"). `kinds` maps attribute → kind.
    """
    raw, choices = dict(raw), {}
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if kinds.get(name) == "colour":
            opts = colour_options(value)
            if opts:
                raw[name] = opts[0]
                choices[name] = opts
        elif colours_only:
            continue
        elif name == "content_item":
            res = content_options(value, category, exclude_contents)
            if res:
                raw[name], choices[name] = res
        elif name == "brand":
            opts = brand_options(value, category)
            if opts:
                raw[name] = opts[0]
                choices[name] = opts
    return raw, choices
