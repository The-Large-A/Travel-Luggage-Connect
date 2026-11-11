"""
The v1 matcher (fixed weights), kept ONLY as the baseline that
tools/match_benchmark.py and tests/test_matching.py compare the v2 matcher
(matching.py) against. Not used by the app.

    final = 0.5·text + 0.3·image + 0.2·route + ocr + structured   (capped at 1.0)
    category clash → final × 0.5; is_match when final >= 0.30
"""


# ──────────────────────────────────────────────
# 2.6 OCR MATCH SCORING
# ──────────────────────────────────────────────
# Generic descriptors that must never, on their own, imply a match.
# (Only words longer than 4 chars are scored, so short words are already
#  excluded — this list catches the common *long* generic words.)
_OCR_STOPWORDS = {
    "black", "white", "brown", "green", "yellow", "orange", "purple",
    "golden", "silver", "colour", "color", "small", "large", "medium",
    "heavy", "light", "handbag", "luggage", "suitcase", "backpack",
    "wallet", "purse", "pouch", "cover", "strap", "zipper", "inside",
}


def ocr_match_score(name: str, description: str, ocr_text: str) -> float:
    """
    Score how strongly text read off the item (OCR) confirms a lost report.

    Why this is NOT a blunt 1.0 boost:
      A bag tag showing the passenger's full name is near-certain proof, so
      that still scores 1.0. But a single generic description word (e.g.
      "black") appearing on the bag is weak evidence and must not, by itself,
      clear the match threshold or trigger a high-confidence alert. So keyword
      hits are filtered (stop-words + length) and require corroboration:
        • full name found in OCR text      → 1.0  (strong)
        • 2+ distinct significant keywords  → 1.0  (strong)
        • exactly 1 significant keyword     → 0.4  (weak bonus)
        • otherwise                         → 0.0

    Returns a float in [0.0, 1.0].
    """
    if not ocr_text:
        return 0.0
    ocr_text = ocr_text.lower()

    name = (name or "").lower().strip()
    if len(name) >= 4 and name in ocr_text:
        return 1.0

    significant = {
        w for w in (description or "").lower().split()
        if len(w) > 4 and w.isalpha() and w not in _OCR_STOPWORDS
    }
    hits = sum(1 for w in significant if w in ocr_text)
    if hits >= 2:
        return 1.0
    if hits == 1:
        return 0.4
    return 0.0


# ──────────────────────────────────────────────
# 2.7 STRUCTURED (TRAVEL-RECORD) MATCH SCORING
# ──────────────────────────────────────────────
def _seat_num(v):
    """Best-effort parse of a seat value ('12', 'A5', 12) → int or None."""
    if v is None:
        return None
    import re as _re
    m = _re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def structured_match_score(found_trip_id, lost_trip_id,
                           found_bus_id, lost_bus_id,
                           found_seat, lost_seat) -> float:
    """
    Score exact travel-record evidence linking a found item to a lost report,
    using the transport database keys the passenger and depot now capture.

    Precedence (strongest wins):
        • same trip (route + bus + departure)  → 1.0  (near-proof)
        • same bus (different/absent trip)      → 0.6  (strong)
        • otherwise                              → 0.0
    Plus a small seat-proximity bonus (+0.15) when the item was found within
    two seats of where the passenger sat — but only if trip/bus already agree,
    so a seat number alone can never manufacture a match.

    Returns a float in [0.0, 1.0].
    """
    base = 0.0
    if found_trip_id and lost_trip_id and found_trip_id == lost_trip_id:
        base = 1.0
    elif found_bus_id and lost_bus_id and found_bus_id == lost_bus_id:
        base = 0.6

    if base > 0.0:
        fs, ls = _seat_num(found_seat), _seat_num(lost_seat)
        if fs is not None and ls is not None and abs(fs - ls) <= 2:
            base += 0.15

    return min(1.0, base)


# ──────────────────────────────────────────────
# 3. UNIFIED SCORER
# ──────────────────────────────────────────────
class UnifiedScorer:
    """
    Combines text, image, and route scores into a single match score.

    Formula:
        final_score = (0.5 × text_score)
                    + (0.3 × image_score)
                    + (0.2 × route_score)
                    + ocr_score
                    + structured_score

    Weights rationale:
        - Text (0.5)  : Most reliable signal — descriptions are detailed
        - Image (0.3) : Strong visual evidence when available
        - Route (0.2) : Binary pass/fail based on route logic
        - OCR (+1.0)  : If the passenger's name or exact description keywords are found on the bag, it's a guaranteed match.
        - Structured (+1.0) : Exact travel-record evidence from the transport DB
          (same trip / same bus / adjacent seat). A same-trip hit is near-proof,
          so it can clear the threshold on its own — mirrors the OCR full-name case.

    route_score is always 1.0 or 0.0 (boolean route eligibility).
    When no image is provided, image_score = 0.0 (weight redistributed
    implicitly — text becomes the dominant factor).
    """

    # Minimum final score to consider a report a "match"
    MATCH_THRESHOLD = 0.30

    @staticmethod
    def compute(
        text_score: float,
        image_score: float,
        route_score: float,
        ocr_score: float = 0.0,
        structured_score: float = 0.0
    ) -> dict:
        """
        Compute unified score and return a breakdown dict.

        Returns:
            {
              "text":   0.72,
              "image":  0.65,
              "route":  1.0,
              "ocr":    0.0,
              "structured": 0.6,
              "final":  0.70,
              "is_match": True
            }
        """
        final = (
            (0.5 * text_score) + (0.3 * image_score) + (0.2 * route_score)
            + ocr_score + structured_score
        )
        # Cap at 1.0
        final = min(1.0, float(final))
        final = round(float(final), 4)
        return {
            "text":       round(float(text_score), 4),
            "image":      round(float(image_score), 4),
            "route":      round(float(route_score), 4),
            "ocr":        round(float(ocr_score), 4),
            "structured": round(float(structured_score), 4),
            "final":      final,
            "is_match": final >= UnifiedScorer.MATCH_THRESHOLD
        }


def score_v1(found: dict, lost: dict, text_sim, image_sim=None) -> dict:
    """app.score_lost_found as it was before v2 (after the route filter)."""
    text_score = text_sim.similarity(lost.get("description", ""), found.get("notes", ""))
    image_score = 0.0
    ocr_score = ocr_match_score(lost.get("name", ""), lost.get("description", ""), found.get("ocr_text") or "")
    structured = structured_match_score(found.get("trip_id"), lost.get("trip_id"), found.get("bus_id"),
                                        lost.get("bus_id"), found.get("found_seat_area"), lost.get("seat_no"))
    score = UnifiedScorer.compute(text_score, image_score, route_score=1.0, ocr_score=ocr_score,
                                  structured_score=structured)
    lc, fc = lost.get("category"), found.get("category")
    if lc and fc and "other" not in (lc, fc) and lc != fc:
        score["final"] = round(score["final"] * 0.5, 4)
        score["is_match"] = score["final"] >= UnifiedScorer.MATCH_THRESHOLD
    return score
