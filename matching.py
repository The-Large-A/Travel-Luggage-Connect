"""
matching.py — evidence-based matcher for lost reports ↔ found items (v2).

Why a rewrite. The v1 scorer added fixed weights (0.5·text + 0.3·image +
0.2·route + OCR + trip) and capped the sum at 1.0. In practice that meant:
  * the route term was always 1.0, so +0.2 was free and almost any text cleared
    the 0.30 threshold;
  * "same trip" added +1.0, so EVERY item found on a trip scored 1.0 against
    EVERY lost report on it — a red umbrella auto-notified the owner of a
    black laptop;
  * raw SBERT/CLIP cosines were read as probabilities, though unrelated items
    already score 0.3-0.5 (the CLIP "cone effect": Liang et al. 2022);
  * contradictions (blue vs black, Wildcraft vs Skybags) cost nothing, and
    each pair was judged alone, so five black bags all looked "certain".

The v2 model is record linkage in the Fellegi–Sunter form (as in Splink):
every signal contributes evidence in BITS, log2(P(signal | same item) /
P(signal | different item)), the bits are summed onto a prior, and the total
M becomes a probability P = 2^M / (1 + 2^M).

  * missing data is neutral (0 bits); a contradiction is negative;
  * rare agreements weigh more than common ones (term frequency: a red match
    is worth more than a black one);
  * correlated signals are grouped and capped, so one description cannot be
    counted three times (SBERT + colour words + contents words);
  * embedding similarities are calibrated against the OTHER candidates in the
    same block (a z-score, shrunk towards the model's null distribution),
    which removes the cone effect and hubness;
  * auto-notify needs more than a high P: at least one identifying signal, no
    contradiction, and a clear lead over the runner-up (set by `gate`).

The module is pure: no database, no Flask. app.py does the blocking (route,
date window, luggage direction) and persistence.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime

from claims.normalize import COLOUR_SET, normalize_text, tokens
from vocabulary import (BRAND_HOMES, COLOUR_PRIOR, COMPATIBLE_CATEGORIES, MATERIALS, NOUN_CATEGORY,
                        colours_confusable)

VERSION = 2
PRIOR_BITS = -3.0                 # before any evidence: about 1 chance in 9 that a candidate is the item
POSSIBLE_MIN = 0.30               # P below this is not recorded as a match at all
AUTO_MARGIN_BITS = 2.0            # the top candidate must lead the runner-up by 4x odds
REVIEW_TOP_N = 2                  # only the leading contenders of a block go to the staff queue
IDENTIFYING = "identifying"

# Null distributions (mean, sd) of each similarity for DIFFERENT items, used to
# turn a raw cosine into a z-score when the block is too small to measure it.
# Measured on the matching benchmark (tools/match_benchmark.py); a similarity
# object can override them with a `null_stats` attribute.
TEXT_NULL = (0.29, 0.15)          # all-MiniLM-L6-v2, lost description vs found notes (measured)
IMAGE_NULL = (0.62, 0.08)         # CLIP ViT-B/32, photo vs photo
CROSS_NULL = (0.21, 0.025)        # CLIP ViT-B/32, description vs photo
SHRINK_K = 4                      # pseudo-observations of the null when calibrating on a block

# Group caps (lower, upper) in bits.
CAPS = {"journey": (-4.0, 4.5), "category": (-6.0, 2.0), "description": (-6.0, 6.0),
        "brand": (-3.0, 4.5), "identifier": (0.0, 10.0), "image": (-1.5, 4.0)}

_SERIAL = re.compile(r"(?<![a-z0-9])(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{5,}(?![a-z0-9])")
_DIGITS = re.compile(r"\d{6,}")
_OCR_STOP = {"made", "china", "india", "size", "warranty", "quality", "brand", "original", "model", "style",
             "fashion", "premium", "genuine", "product", "water", "resistant", "bag", "bags", "with", "inside",
             "black", "colour", "color", "from", "this", "that", "item", "found", "name", "cover", "case",
             "sticker", "label", "written", "says", "small", "big", "large", "new", "old", "left", "lost"}
COMMON_NAME_TOKENS = {"kumar", "raj", "devi", "lakshmi", "priya", "selvi", "murugan", "rani", "mani", "babu",
                      "senthil", "kannan", "arun", "raja", "ravi", "anand", "kavitha", "meena", "suresh",
                      "ramesh", "vijay", "karthik", "sri", "shree", "mohan", "ganesh", "bala", "siva"}


# ── facts extracted from free text ───────────────────────────────────────────
class Facts:
    """What a description says, in canonical tokens."""

    __slots__ = ("category", "colours", "brands", "contents", "materials", "identifiers", "digits", "words",
                 "text")

    def __init__(self, text: str):
        self.text = text if isinstance(text, str) else ""
        norm = normalize_text(self.text)
        toks = tokens(self.text)
        self.category = next((NOUN_CATEGORY[t] for t in toks if t in NOUN_CATEGORY), None)
        self.colours = tuple(t for t in toks if t in COLOUR_SET)
        self.brands = frozenset(b for b in BRAND_HOMES if re.search(rf"(?<!\w){re.escape(normalize_text(b))}(?!\w)",
                                                                    norm))
        item_seen = False
        contents = []
        for t in toks:                                   # every category noun after the first is content
            if t in NOUN_CATEGORY and not item_seen:
                item_seen = True
                continue
            if t in _CONTENT_WORDS:
                contents.append(t)
        self.contents = frozenset(contents)
        self.materials = frozenset(t for t in norm.split() if t in MATERIALS)
        self.identifiers = frozenset(_SERIAL.findall(norm))
        self.digits = frozenset(_DIGITS.findall(re.sub(r"[\s\-]", "", self.text)))
        brand_words = {w for b in self.brands for w in normalize_text(b).split()}
        self.words = frozenset(t for t in toks if len(t) >= 4 and t.isalpha() and t not in _OCR_STOP
                               and t not in COLOUR_SET and t not in NOUN_CATEGORY and t not in _CONTENT_WORDS
                               and t not in brand_words)


def _content_words():
    from vocabulary import CONTENTS
    return frozenset(CONTENTS) | {"phone", "wallet", "laptop", "spectacles", "umbrella"}


_CONTENT_WORDS = _content_words()


def _days_between(a: str, b: str):
    try:
        return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def _seat(v):
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def _z(value, block, null, shrink=SHRINK_K):
    """z-score of `value` against the other candidates, shrunk towards the null."""
    mu0, sd0 = null
    if not block:
        return (value - mu0) / sd0
    n = len(block)
    mu = (shrink * mu0 + sum(block)) / (shrink + n)
    var = (shrink * (sd0 ** 2 + (mu0 - mu) ** 2) + sum((x - mu) ** 2 for x in block)) / (shrink + n)
    return (value - mu) / max(math.sqrt(var), sd0 * 0.5)


def _z_bits(z, top=3.0):
    if z >= 3:
        return top
    if z >= 2:
        return top * 2 / 3
    if z >= 1:
        return top / 3
    if z >= -1:
        return 0.0
    return -1.0


def probability(bits: float) -> float:
    return 1.0 / (1.0 + 2.0 ** (-bits))


class Evidence:
    def __init__(self):
        self.items = []                                  # (group, signal, bits, kind, note)

    def add(self, group, signal, bits, note, kind="support"):
        if bits or kind == IDENTIFYING:
            self.items.append((group, signal, round(float(bits), 2), kind, note))

    def total(self):
        by_group = {}
        for group, _s, bits, _k, _n in self.items:
            by_group[group] = by_group.get(group, 0.0) + bits
        return sum(min(max(v, CAPS[g][0]), CAPS[g][1]) for g, v in by_group.items()), by_group

    def kinds(self, kind):
        return [s for _g, s, _b, k, _n in self.items if k == kind]


# ── the matcher ──────────────────────────────────────────────────────────────
class Matcher:
    """
    text_sim / image_sim: the app's similarity objects (or test fakes).
    `image_root` is where stored relative image paths live ("static").
    """

    def __init__(self, text_sim, image_sim, image_root="static"):
        self.text_sim, self.image_sim, self.image_root = text_sim, image_sim, image_root

    # raw similarities ---------------------------------------------------------
    def _paths(self, doc, found=True):
        if found:
            rel = doc.get("images") or ([doc["image_path"]] if doc.get("image_path") else [])
        else:
            rel = [doc["image_path"]] if doc.get("image_path") else []
        return [os.path.join(self.image_root, p) for p in rel[:8]]

    def raw(self, found, lost) -> dict:
        desc, notes = _text(lost.get("description")), found_text(found)
        text = self.text_sim.similarity(desc, notes) if desc.strip() and notes.strip() else None
        image = cross = None
        if getattr(self.image_sim, "available", False):
            fpaths, lpaths = self._paths(found), self._paths(lost, found=False)
            if fpaths and lpaths:
                image = max(self.image_sim.similarity(f, lpaths[0]) for f in fpaths)
            text_to_images = getattr(self.image_sim, "text_to_images", None)
            if fpaths and text_to_images and desc.strip():
                cross = text_to_images(desc, fpaths)
        return {"text": text, "image": image, "cross": cross}

    # scoring ------------------------------------------------------------------
    def score(self, found, lost, raw=None, background=None) -> dict:
        """
        Score one (found, lost) pair. `raw` are precomputed similarities;
        `background` maps "text"/"image"/"cross" to the same similarity of the
        OTHER candidates in the block (for calibration).
        """
        raw = raw if raw is not None else self.raw(found, lost)
        background = background or {}
        ev = Evidence()
        lf, ff = Facts(_text(lost.get("description"))), Facts(found_text(found))
        self._journey(ev, found, lost, background.get("crowd", 1))
        lost_cat, found_cat, cat_conf = self._categories(found, lost, lf, ff)
        self._category(ev, lost_cat, found_cat, cat_conf)
        self._colours(ev, found, lf, ff)
        self._contents(ev, lf, ff, lost_cat or found_cat)
        self._materials(ev, lf, ff)
        self._brands(ev, lf, ff, found, lost_cat, found_cat)
        self._identifiers(ev, found, lost, lf)
        stats = self._similarities(ev, raw, background)
        self._distinctive(ev, stats)

        total, groups = ev.total()
        bits = PRIOR_BITS + total
        p = probability(bits)
        conflicts = ev.kinds("conflict")
        return {
            "version": VERSION,
            # v1-compatible fields shown on the depot page
            "text": round(float(raw.get("text") or 0.0), 4), "image": round(float(raw.get("image") or 0.0), 4),
            "route": 1.0, "ocr": round(min(1.0, max(0.0, groups.get("identifier", 0.0)) / 8), 4),
            "structured": round(min(1.0, max(0.0, groups.get("journey", 0.0)) / 4), 4),
            "final": round(p, 4), "is_match": p >= POSSIBLE_MIN,
            # v2 explanation
            "bits": round(bits, 2), "prior": PRIOR_BITS,
            "groups": {g: round(min(max(v, CAPS[g][0]), CAPS[g][1]), 2) for g, v in groups.items()},
            "evidence": [{"group": g, "signal": s, "bits": b, "kind": k, "note": n}
                         for g, s, b, k, n in sorted(ev.items, key=lambda t: -abs(t[2]))],
            "conflicts": conflicts, "identifying": ev.kinds(IDENTIFYING), "calibration": stats,
            "categoryMismatch": "category_clash" in conflicts,
            "autoEligible": None, "reviewEligible": None, "rank": None, "blockers": [], "margin": None,
        }

    # journey: trip, bus, seat, dates -------------------------------------------
    def _journey(self, ev, found, lost, crowd=1):
        """
        `crowd` = how many candidates in this block share the trip (or bus):
        being on the same trip is strong evidence when one item was found on
        it and weak when seven were (term-frequency adjustment, as in Splink).
        """
        ft, lt, fb, lb = found.get("trip_id"), lost.get("trip_id"), found.get("bus_id"), lost.get("bus_id")
        same_bus = None
        crowd_bits = math.log2(max(1, crowd))
        crowd_note = f", one of {crowd} on it" if crowd > 1 else ""
        same_trip = bool(ft and lt and ft == lt)
        if fb and lb:
            same_bus = fb == lb
        if same_trip:
            ev.add("journey", "same_trip", max(0.5, 3.0 - crowd_bits), "found on the passenger's own trip" + crowd_note)
            same_bus = True
        elif same_bus:
            base = 2.0 if not (ft and lt) else 1.0
            ev.add("journey", "same_bus", max(0.3, base - crowd_bits),
                   "found on the passenger's bus" + (" (a later run)" if ft and lt else "") + crowd_note)
        elif same_bus is False:
            ev.add("journey", "different_bus", -3.0, "found on a different bus", "conflict")
        if same_bus:
            fs, ls = _seat(found.get("found_seat_area")), _seat(lost.get("seat_no"))
            if fs is not None and ls is not None:
                if abs(fs - ls) <= 2:
                    ev.add("journey", "seat", 1.0, f"found within 2 seats of seat {ls}")
                elif abs(fs - ls) <= 6:
                    ev.add("journey", "seat", 0.3, "found a few rows from the passenger's seat")
        gap = _days_between(lost.get("date"), found.get("date"))
        if gap is None:
            return
        if same_trip and gap in (0, 1):
            return                                       # already counted: the trip fixes the day
        if gap < -1:
            ev.add("journey", "date", -3.0, f"logged {-gap} days BEFORE the journey", "conflict")
        elif gap == -1:
            ev.add("journey", "date", -0.5, "logged the day before the journey date")
        elif gap == 0:
            ev.add("journey", "date", 1.0, "found the same day")
        elif gap == 1:
            ev.add("journey", "date", 0.5, "found the next day")
        elif gap >= 4:
            ev.add("journey", "date", -0.5, f"found {gap} days later")

    # category ---------------------------------------------------------------
    @staticmethod
    def _categories(found, lost, lf, ff):
        lost_cat = lost.get("category") if lost.get("category") not in (None, "", "other") else lf.category
        found_cat = found.get("category") if found.get("category") not in (None, "", "other") else ff.category
        conf = ((found.get("attributes") or {}).get("category") or {}).get("confidence")
        if found.get("captureMode") != "camera_scan" or conf is None:
            conf = 1.0 if found.get("category") not in (None, "", "other") else 0.7
        return lost_cat, found_cat, float(conf)

    @staticmethod
    def _category(ev, lost_cat, found_cat, conf):
        if not lost_cat or not found_cat:
            return
        if lost_cat == found_cat:
            ev.add("category", "category", 2.0, f"both are a {lost_cat}")
        elif frozenset((lost_cat, found_cat)) in COMPATIBLE_CATEGORIES:
            ev.add("category", "category", -1.0, f"{lost_cat} vs {found_cat} (often filed either way)")
        elif conf < 0.6:
            ev.add("category", "category", -1.5, f"{lost_cat} vs {found_cat} (camera was unsure)")
        else:
            ev.add("category", "category_clash", -6.0, f"a {lost_cat} was lost, a {found_cat} was found",
                   "conflict")

    # colours ----------------------------------------------------------------
    @staticmethod
    def _found_colours(found, ff):
        scanned = [c["name"] for c in ((found.get("attributes") or {}).get("colours") or [])
                   if isinstance(c, dict) and c.get("share", 0) >= 0.1 and c.get("name") in COLOUR_SET]
        return tuple(dict.fromkeys(scanned + list(ff.colours)))

    def _colours(self, ev, found, lf, ff):
        lc, fc = set(lf.colours), set(self._found_colours(found, ff))
        if not lc or not fc:
            return
        shared = lc & fc
        if shared:
            best = max(shared, key=lambda c: -COLOUR_PRIOR.get(c, 0.05))         # the rarest shared colour
            bits = min(3.5, math.log2(0.85 / COLOUR_PRIOR.get(best, 0.05)))
            if len(shared) >= 2:
                bits += 0.5
            ev.add("description", "colour", bits, f"both {' and '.join(sorted(shared))}")
        elif any(colours_confusable(a, b) for a in lc for b in fc):
            ev.add("description", "colour", -0.5, f"{'/'.join(sorted(lc))} vs {'/'.join(sorted(fc))} (close shades)")
        else:
            ev.add("description", "colour_conflict", -2.5,
                   f"{'/'.join(sorted(lc))} was lost, {'/'.join(sorted(fc))} was found", "conflict")

    # contents and material ------------------------------------------------------
    @staticmethod
    def _contents(ev, lf, ff, category):
        ignore = {category} if category else set()
        lc, fc = lf.contents - ignore, ff.contents - ignore
        if not lc or not fc:
            return
        shared = lc & fc
        if shared:
            ev.add("description", "contents", min(3.5, 1.5 * len(shared)), f"both mention {', '.join(sorted(shared))}",
                   IDENTIFYING if len(shared) >= 2 else "support")
        else:
            # Staff list what they can see, so a miss is possible: P(no overlap | same) ~ 0.3,
            # P(no overlap | different) ~ 0.9, so log2(0.3 / 0.9) ~ -1.6 bits.
            ev.add("description", "contents", -1.6, "different contents listed")

    @staticmethod
    def _materials(ev, lf, ff):
        if lf.materials and ff.materials:
            if lf.materials & ff.materials:
                ev.add("description", "material", 0.5, f"both {', '.join(sorted(lf.materials & ff.materials))}")
            else:
                ev.add("description", "material", -1.0, "different material")

    # brands -----------------------------------------------------------------
    @staticmethod
    def _brands(ev, lf, ff, found, lost_cat, found_cat):
        found_brands = set(ff.brands)
        ocr = normalize_text(_text(found.get("ocr_text")))
        found_brands |= {b for b in BRAND_HOMES if re.search(rf"(?<!\w){re.escape(normalize_text(b))}(?!\w)", ocr)}

        def split(brands, category):
            """The item's own brand (it makes this category) vs brands of things inside it."""
            own = {b for b in brands if not category or category in BRAND_HOMES[b]}
            return own, set(brands) - own
        l_own, l_other = split(lf.brands, lost_cat or found_cat)
        f_own, f_other = split(found_brands, found_cat or lost_cat)
        if l_own and f_own:
            if l_own & f_own:
                ev.add("brand", "brand", 4.0, f"same brand: {', '.join(sorted(l_own & f_own)).title()}", IDENTIFYING)
            else:
                ev.add("brand", "brand_conflict", -3.0,
                       f"{', '.join(sorted(l_own)).title()} was lost, {', '.join(sorted(f_own)).title()} was found",
                       "conflict")
        if l_other & f_other:
            ev.add("brand", "content_brand", 1.5, f"same brand inside: {', '.join(sorted(l_other & f_other)).title()}")

    # identifiers: serials, phone numbers, the passenger's name ------------------
    @staticmethod
    def _identifiers(ev, found, lost, lf):
        found_all = f"{found_text(found)} {_text(found.get('ocr_text'))}"
        ff = Facts(found_all)
        serials = lf.identifiers & ff.identifiers
        if serials:
            ev.add("identifier", "serial", 8.0, "the same serial / ID number", IDENTIFYING)
        digits_found = re.sub(r"\D", "", found_all)
        phone = re.sub(r"\D", "", _text(lost.get("phone")))[-10:]
        if (len(phone) == 10 and phone in digits_found) or (lf.digits and any(d in digits_found for d in lf.digits)):
            ev.add("identifier", "number", 8.0, "the passenger's number is written on the item", IDENTIFYING)
        name = normalize_text(_text(lost.get("name")))
        text = f" {normalize_text(found_all)} "
        parts = [p for p in name.split() if len(p) >= 3]
        if len(parts) >= 2 and f" {' '.join(parts)} " in text:
            ev.add("identifier", "name", 8.0, "the passenger's full name is on the item", IDENTIFYING)
        else:
            hits = [p for p in parts if len(p) >= 4 and f" {p} " in text]
            if hits:
                rare = [p for p in hits if p not in COMMON_NAME_TOKENS]
                ev.add("identifier", "name_part", 3.0 if rare else 1.0, "part of the passenger's name is on the item")
        ocr_words = set(normalize_text(_text(found.get("ocr_text"))).split())
        rare_hits = {w for w in lf.words if w in ocr_words}
        if rare_hits:
            ev.add("identifier", "ocr_words", min(3.0, 1.5 * len(rare_hits)),
                   f"words read off the item: {', '.join(sorted(rare_hits))}")

    @staticmethod
    def _distinctive(ev, stats):
        """
        A rare colour on the same kind of item, on the passenger's own journey,
        with descriptions that clearly agree (z >= 2) and nothing contradicting
        it, identifies an item as well as a brand does: "the only red umbrella
        found on your bus". Black, blue and grey are too common for this.
        """
        signals = {s: b for _g, s, b, _k, _n in ev.items}
        rare_colour = signals.get("colour", 0) >= 3.0
        same_journey = "same_trip" in signals or "same_bus" in signals
        if rare_colour and same_journey and signals.get("category", 0) > 0 and stats.get("textZ", 0) >= 2 \
                and not ev.kinds("conflict"):
            ev.add("description", "distinctive", 0.0, "a rare colour on the same kind of item, on the same journey",
                   IDENTIFYING)

    # calibrated embeddings ------------------------------------------------------
    def _similarities(self, ev, raw, background):
        stats = {}
        null = getattr(self.text_sim, "null_stats", TEXT_NULL)
        if raw.get("text") is not None:
            z = _z(raw["text"], background.get("text"), null)
            stats["textZ"] = round(z, 2)
            ev.add("description", "text", _z_bits(z), f"descriptions read alike (z={z:.1f})" if z >= 1
                   else "descriptions read differently" if z < -1 else "descriptions: no clear signal")
        img_null = getattr(self.image_sim, "null_stats", IMAGE_NULL)
        if raw.get("image") is not None:
            z = _z(raw["image"], background.get("image"), img_null)
            stats["imageZ"] = round(z, 2)
            kind = IDENTIFYING if z >= 3 and raw["image"] >= 0.92 else "support"
            ev.add("image", "photo", _z_bits(z, 4.0), f"photos look alike (z={z:.1f})" if z >= 1
                   else "photos look different" if z < -1 else "photos: no clear signal", kind)
        cross_null = getattr(self.image_sim, "cross_null_stats", CROSS_NULL)
        if raw.get("cross") is not None:
            z = _z(raw["cross"], background.get("cross"), cross_null)
            stats["crossZ"] = round(z, 2)
            ev.add("image", "description_vs_photo", _z_bits(z, 2.0),
                   f"the description fits the item photos (z={z:.1f})" if z >= 1 else
                   "the description does not fit the photos" if z < -1 else "description vs photos: no clear signal")
        return stats

    # blocks: calibrate against the other candidates, then gate auto-notify -------
    def score_block(self, query, candidates, query_is_found: bool) -> list:
        """
        Score `query` against every candidate (the other side). Returns
        [(candidate, score)] best first, with margins and auto gates applied.
        """
        pairs = [(c, (query, c) if query_is_found else (c, query)) for c in candidates]
        raws = [self.raw(f, l) for _c, (f, l) in pairs]
        keys = [_journey_key(c) for c in candidates]
        out = []
        for i, (cand, (f, l)) in enumerate(pairs):
            background = {k: [r[k] for j, r in enumerate(raws) if j != i and r.get(k) is not None]
                          for k in ("text", "image", "cross")}
            background["crowd"] = sum(1 for k in keys if keys[i] and k == keys[i]) or 1
            out.append((cand, self.score(f, l, raws[i], background)))
        out.sort(key=lambda t: -t[1]["bits"])
        gate([s for _c, s in out])
        return out


def _journey_key(doc):
    """Trip if known, else bus: the unit 'how many items share this journey' is counted in."""
    if doc.get("trip_id"):
        return ("trip", doc["trip_id"])
    if doc.get("bus_id"):
        return ("bus", doc["bus_id"])
    return None


def is_contested(matcher, anchor, query, rivals, anchor_is_found: bool) -> bool:
    """
    Mutual-best check. `query` is the auto-notify candidate for `anchor`; the
    rivals are the other docs on the query's side that could also belong to the
    anchor. Contested when any rival scores within AUTO_MARGIN_BITS of the query
    against the anchor (all scored in one block, so calibration is shared).
    """
    if not rivals:
        return False
    block = matcher.score_block(anchor, [query] + list(rivals), query_is_found=anchor_is_found)
    q_bits = next(s["bits"] for c, s in block if c is query)
    return any(s["bits"] >= q_bits - AUTO_MARGIN_BITS for c, s in block if c is not query)


def gate(scores: list, contested=lambda score: False) -> None:
    """
    Decide auto-notify eligibility for a ranked block (best first), in place.
    Auto needs: an identifying signal, no contradiction, a lead of at least
    AUTO_MARGIN_BITS over the runner-up, and (via `contested`) that no other
    report/item claims this one as strongly.
    """
    for rank, s in enumerate(scores):
        runner_up = scores[1]["bits"] if rank == 0 and len(scores) > 1 else (scores[0]["bits"] if rank else None)
        s["margin"] = None if runner_up is None else round(s["bits"] - runner_up, 2)
        blockers = []
        if not s["identifying"]:
            blockers.append("no identifying detail (brand, serial, name, contents or a near-identical photo)")
        if s["conflicts"]:
            blockers.append("contradiction: " + ", ".join(s["conflicts"]))
        if rank > 0:
            blockers.append("another candidate scores higher")
        elif s["margin"] is not None and s["margin"] < AUTO_MARGIN_BITS:
            blockers.append("a second candidate is almost as likely")
        s["blockers"] = blockers
        s["autoEligible"] = not blockers
        s["rank"] = rank + 1
        # Staff see the leading contenders, not every black bag on the trip:
        # the top two, plus anything within a bit of the leader.
        s["reviewEligible"] = rank < REVIEW_TOP_N or (scores[0]["bits"] - s["bits"]) <= 1.0
    if scores and scores[0]["autoEligible"] and contested(scores[0]):
        scores[0]["autoEligible"] = False
        scores[0]["blockers"].append("this item is also a strong match for another report")


def route(score, auto_min: float, review_min: float):
    """
    The routing decision for a v2 score: "auto", "review", "reject" (recorded,
    never shown to the passenger) or None (not worth recording).
    """
    p = score["final"]
    if p >= auto_min and score.get("autoEligible"):
        return "auto"
    if p >= review_min and score.get("reviewEligible", True) is not False:
        return "review"
    return "reject" if p >= POSSIBLE_MIN else None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def found_text(found) -> str:
    """The found item's private description the matcher compares with lost reports."""
    return _text(found.get("notes")) or _text(found.get("public_description"))


def summary(score) -> list:
    """Short human reasons for staff (strongest first)."""
    return [e["note"] for e in score.get("evidence", []) if abs(e["bits"]) >= 1 or e["kind"] == IDENTIFYING][:5]


__all__ = ["Matcher", "Facts", "gate", "route", "is_contested", "probability", "summary", "found_text",
           "POSSIBLE_MIN", "VERSION",
           "PRIOR_BITS", "AUTO_MARGIN_BITS"]
