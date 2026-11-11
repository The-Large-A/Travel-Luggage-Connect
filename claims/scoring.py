"""Per-question outcomes and the weighted trust score."""
from .normalize import colour_tokens, match_ratio, token_hash, tokens

VERIFIED, BORDERLINE, FAIL = "verified", "borderline", "fail"


def answer_passes(key, attribute: dict, answer: str, config, strict=False, generic=()) -> bool:
    """
    True when the submitted answer matches the stored attribute.
    Colours: any of at most two named colours must equal the expected colour.
    Text: at least CLAIM_ANSWER_MATCH_MIN of the expected words (typo-tolerant).
    `strict` (the extra borderline question): no typo tolerance and 2/3 of the words.
    `generic`: words the question itself suggests; they are ignored on BOTH sides
    (the stored entries are matched by hash), so "sticker" is no answer to
    "Describe a sticker…", while "CSK sticker" still matches "csk sticker".
    """
    entries = attribute["entries"]
    if attribute["kind"] == "colour":
        named = colour_tokens(answer)
        if len(named) > 2:
            answer_toks = []                       # listing every colour is not an answer
        else:
            answer_toks = named or tokens(answer)  # an unusual colour word stored verbatim
        minimum = 1.0
    else:
        answer_toks = tokens(answer)
        minimum = max(config.answer_match_min, 0.67) if strict else config.answer_match_min
        if generic:
            generic_hashes = {token_hash(key, g) for g in generic}
            specific = [e for e in entries if e["h"] not in generic_hashes]
            if specific:                                   # the true answer says more than "sticker"
                entries = specific
                answer_toks = [t for t in answer_toks if t not in generic]
    ratio = match_ratio(key, entries, answer_toks, fuzzy=not strict,
                        fuzzy_min=config.fuzzy_min_token_length)
    return ratio >= minimum - 1e-9


def trust_score(outcomes: dict, weights: dict) -> float:
    total = sum(weights[q] for q in outcomes)
    if not total:
        return 0.0
    return round(sum(weights[q] for q, ok in outcomes.items() if ok) / total, 4)


def round_verdict(outcomes: dict, weights: dict, hidden, config):
    """
    (trust score, verdict) for one round. A round can only be VERIFIED when every
    hidden-knowledge question in it (what was inside, the lock screen, the name on
    the tag) was answered correctly: facts visible from outside are never enough.
    """
    score = trust_score(outcomes, weights)
    verdict = decide(score, config)
    asked_hidden = [ok for q, ok in outcomes.items() if q in hidden]
    if verdict == VERIFIED and (not asked_hidden or not all(asked_hidden)):
        verdict = BORDERLINE          # nothing hidden asked (e.g. an umbrella) → staff check the ID
    return score, verdict


def decide(score: float, config) -> str:
    if score >= config.trust_threshold:
        return VERIFIED
    if score >= config.trust_review_min:
        return BORDERLINE
    return FAIL
