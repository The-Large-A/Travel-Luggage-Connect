"""
The v2 matcher (matching.py): fact extraction, every evidence rule, the
calibration, the auto-notify gates, robustness against odd input, and the
labelled benchmark that compares it with the v1 fixed-weight scorer.
"""
import functools
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import match_cases as cases                                     # noqa: E402
from match_benchmark import NoImages, WordOverlap, metrics       # noqa: E402
from matching import (AUTO_MARGIN_BITS, CAPS, POSSIBLE_MIN, PRIOR_BITS, Facts, Matcher, gate,  # noqa: E402
                      is_contested, probability, route, summary)


class FixedText:
    """Text similarity that returns whatever the test sets per (lost, found) text pair."""
    null_stats = (0.3, 0.1)

    def __init__(self, default=0.3, table=None):
        self.default, self.table = default, table or {}

    def similarity(self, a, b):
        return self.table.get((a, b), self.default)


class FakeImages:
    available = True
    null_stats = (0.6, 0.08)
    cross_null_stats = (0.2, 0.03)

    def __init__(self, photo=None, cross=None):
        self.photo, self.cross = photo or {}, cross or {}

    def similarity(self, f, lo):
        return self.photo.get(f.replace("\\", "/").split("/")[-1], 0.6)

    def text_to_images(self, text, paths):
        return self.cross.get(text, 0.2)


def M(text=None, images=None):
    return Matcher(text or FixedText(), images or NoImages())


def lost(desc="black bag", **kw):
    doc = {"request_id": "r1", "description": desc, "date": "2026-11-01", "name": "Arun Kumar",
           "phone": "+919876543210", "category": None}
    doc.update(kw)
    return doc


def found(notes="black bag", **kw):
    doc = {"found_id": "f1", "notes": notes, "date": "2026-11-01", "ocr_text": "", "category": None}
    doc.update(kw)
    return doc


def signals(score):
    return {e["signal"]: e["bits"] for e in score["evidence"]}


# ── facts ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,category,colours,contents", [
    ("Black backpack with a phone and charger inside", "bag", ("black",), {"phone", "charger"}),
    ("phone in a black cover", "phone", ("black",), set()),                      # the FIRST noun is the item
    ("phone in a black pouch", "phone", ("black",), {"wallet"}),                 # a pouch folds to wallet
    ("karuppu pai, ulle tiffin matrum charger", "bag", ("black",), {"tiffin", "charger"}),   # Tanglish
    ("நீல பை, உள்ளே மருந்து மற்றும் கண்ணாடி", "bag", ("blue",), {"medicine", "spectacles"}),   # Tamil
    ("brown ladies purse, ATM card and bus ticket inside", "wallet", ("brown",), {"card", "ticket"}),
    ("file with certificates", "documents", (), {"certificate"}),
    ("", None, (), set()),
    ("something I lost", None, (), set()),
])
def test_facts_from_free_text(text, category, colours, contents):
    f = Facts(text)
    assert f.category == category and f.colours == colours and set(f.contents) == contents


def test_facts_brands_serials_and_numbers():
    f = Facts("HP laptop, serial 5CG1234XYZ, call 98765 43210, Wildcraft sleeve")
    assert f.brands == {"hp", "wildcraft"} and "5cg1234xyz" in f.identifiers and "9876543210" in f.digits
    assert Facts("model 2023 size 15").identifiers == frozenset()                # numbers alone are not serials
    assert "wildcraft" not in Facts("Wildcraft bag with CSK sticker").words      # brands are not "rare words"


# ── journey evidence ───────────────────────────────────────────────────────────
def test_same_trip_alone_is_never_enough():
    s = M().score(found("a bag", trip_id="T1"), lost("an umbrella", trip_id="T1"))
    assert signals(s)["same_trip"] == 3.0 and s["final"] < 0.9
    gate([s])
    assert not s["autoEligible"] and route(s, 0.85, 0.6) != "auto"


def test_same_trip_counts_less_when_the_trip_is_crowded():
    m = M()
    lo = lost("black bag", trip_id="T1")
    alone = m.score_block(lo, [found("black bag", trip_id="T1")], query_is_found=False)[0][1]
    crowd = m.score_block(lo, [found("black bag", trip_id="T1", found_id=f"f{i}") for i in range(8)],
                          query_is_found=False)[0][1]
    assert signals(alone)["same_trip"] == 3.0 and signals(crowd)["same_trip"] == 0.5     # 3 - log2(8), floored


def test_different_bus_is_a_contradiction_and_missing_journey_is_neutral():
    s = M().score(found(bus_id="B2"), lost(bus_id="B1"))
    assert "different_bus" in s["conflicts"]
    neutral = M().score(found(), lost())
    assert not any(e["group"] == "journey" and e["signal"] != "date" for e in neutral["evidence"])


def test_later_run_of_the_same_bus_still_counts():
    s = M().score(found(trip_id="T2", bus_id="B1"), lost(trip_id="T1", bus_id="B1"))
    assert signals(s)["same_bus"] == 1.0 and not s["conflicts"]


@pytest.mark.parametrize("found_date,bits,conflict", [
    ("2026-11-01", 1.0, False), ("2026-11-02", 0.5, False), ("2026-11-03", None, False),
    ("2026-11-06", -0.5, False), ("2026-10-31", -0.5, False), ("2026-10-29", -3.0, True),
])
def test_date_gaps(found_date, bits, conflict):
    s = M().score(found(date=found_date), lost())
    assert signals(s).get("date") == bits and (("date" in s["conflicts"]) is conflict)


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-13-45", 20261101])
def test_malformed_dates_are_neutral(bad):
    s = M().score(found(date=bad), lost())
    assert "date" not in signals(s)


def test_seat_proximity_only_counts_on_the_same_bus():
    near = M().score(found(trip_id="T1", found_seat_area=13), lost(trip_id="T1", seat_no="12"))
    assert signals(near)["seat"] == 1.0
    other_bus = M().score(found(bus_id="B2", found_seat_area=13), lost(bus_id="B1", seat_no=12))
    assert "seat" not in signals(other_bus)


# ── item evidence ──────────────────────────────────────────────────────────────
def test_category_agreement_compatibility_and_clash():
    assert signals(M().score(found(category="bag"), lost(category="bag")))["category"] == 2.0
    purse = M().score(found("brown purse", category="wallet"), lost("brown purse", category="bag"))
    assert signals(purse)["category"] == -1.0 and not purse["conflicts"]          # purse: wallet or handbag
    clash = M().score(found("x", category="umbrella"), lost("x", category="laptop"))
    assert "category_clash" in clash["conflicts"] and clash["categoryMismatch"]


def test_an_unsure_camera_category_is_not_a_contradiction():
    unsure = found("x", category="umbrella", captureMode="camera_scan",
                   attributes={"category": {"value": "umbrella", "confidence": 0.41}})
    s = M().score(unsure, lost("x", category="laptop"))
    assert not s["conflicts"] and signals(s)["category"] == -1.5


def test_rare_colours_weigh_more_than_black():
    black = signals(M().score(found("black bag"), lost("black bag")))["colour"]
    red = signals(M().score(found("red bag"), lost("red bag")))["colour"]
    assert 1.0 < black < red <= 3.5


def test_close_shades_are_not_a_contradiction_but_opposites_are():
    navy = M().score(found("black backpack"), lost("navy backpack"))           # navy folds to blue ~ black
    assert signals(navy)["colour"] == -0.5 and not navy["conflicts"]
    clash = M().score(found("green suitcase"), lost("red suitcase"))
    assert "colour_conflict" in clash["conflicts"]


def test_scanned_colour_shares_are_used_and_tiny_shares_ignored():
    scan = found("A bag.", attributes={"colours": [{"name": "red", "share": 0.7}, {"name": "green", "share": 0.05}]})
    assert "colour" in signals(M().score(scan, lost("red bag")))
    assert "colour_conflict" in M().score(scan, lost("green bag"))["conflicts"]   # 5% green is noise


def test_contents_overlap_and_disjoint_lists():
    two = M().score(found("bag, tiffin and charger inside"), lost("bag with tiffin and charger"))
    assert signals(two)["contents"] == 3.0 and "contents" in two["identifying"]
    none = M().score(found("bag with clothes and towel"), lost("bag with keys and purse"))
    assert signals(none)["contents"] == -1.6 and not none["conflicts"]


def test_item_brand_vs_brand_of_something_inside():
    same = M().score(found("Wildcraft backpack", category="bag"), lost("Wildcraft backpack", category="bag"))
    assert signals(same)["brand"] == 4.0 and "brand" in same["identifying"]
    clash = M().score(found("Skybags backpack", category="bag"), lost("Wildcraft backpack", category="bag"))
    assert "brand_conflict" in clash["conflicts"]
    inside = M().score(found("Skybags backpack, Dell laptop inside", category="bag"),
                       lost("Wildcraft backpack with Dell laptop", category="bag"))
    assert "brand_conflict" in inside["conflicts"] and signals(inside)["content_brand"] == 1.5
    tablet = M().score(found("Samsung tablet", category="laptop"), lost("Samsung phone", category="phone"))
    assert "brand" not in signals(tablet)                                      # Samsung makes no "laptop" here


def test_brand_read_by_ocr_counts():
    s = M().score(found("A black backpack.", category="bag", ocr_text="WILDCRAFT"),
                  lost("Wildcraft bag", category="bag"))
    assert "brand" in s["identifying"]


def test_identifiers():
    m = M()
    assert "serial" in m.score(found("laptop", ocr_text="SN 5CG1234XYZ"), lost("HP laptop 5CG1234XYZ"))["identifying"]
    assert "number" in m.score(found("bag", ocr_text="call 98765 43210"), lost("bag"))["identifying"]
    assert "name" in m.score(found("bag", ocr_text="ARUN KUMAR"), lost("bag"))["identifying"]
    common = signals(m.score(found("bag", ocr_text="KUMAR"), lost("bag")))
    rare = signals(m.score(found("bag", ocr_text="RAJAGOPALAN"), lost("bag", name="S Rajagopalan")))
    assert common["name_part"] == 1.0 and rare["name_part"] == 3.0            # "Kumar" is everyone in TN
    assert "number" not in signals(m.score(found("bag", ocr_text="MRP 1499"), lost("bag")))


def test_distinctive_combination_identifies_a_lone_red_umbrella():
    text = FixedText(table={("red umbrella", "red umbrella"): 0.95})
    s = M(text).score(found("red umbrella", trip_id="T1", category="umbrella"),
                      lost("red umbrella", trip_id="T1", category="umbrella"))
    assert "distinctive" in s["identifying"]
    black = M(FixedText(table={("black umbrella", "black umbrella"): 0.95})).score(
        found("black umbrella", trip_id="T1", category="umbrella"),
        lost("black umbrella", trip_id="T1", category="umbrella"))
    assert "distinctive" not in black["identifying"]                          # black is too common


# ── calibrated similarities ────────────────────────────────────────────────────
def test_text_similarity_is_read_against_the_block_not_as_a_probability():
    hub = FixedText(default=0.7)                                  # a model that says 0.7 for everything
    m = M(hub)
    block = m.score_block(lost("black bag"), [found("x", found_id=f"f{i}") for i in range(6)],
                          query_is_found=False)
    assert all(signals(s).get("text", 0) <= 1.0 for _f, s in block)           # high but not special → ~no bits
    single = m.score(found("x"), lost("black bag"))
    assert signals(single)["text"] == 3.0                                      # alone, vs the null: strong


def test_photo_and_description_vs_photo():
    imgs = FakeImages(photo={"a.jpg": 0.95}, cross={"red bag": 0.32})
    s = M(images=imgs).score(found("bag", images=["a.jpg"]), lost("red bag", image_path="l.jpg"))
    assert signals(s)["photo"] == 4.0 and "photo" in s["identifying"]
    assert signals(s)["description_vs_photo"] == 1.5                         # capped: mostly re-states the category
    no_photo = M(images=imgs).score(found("bag"), lost("red bag"))
    assert "photo" not in signals(no_photo) and "description_vs_photo" not in signals(no_photo)


def test_group_caps_stop_one_description_counting_three_times():
    text = FixedText(table={("red leather bag with tiffin charger bottle", "red leather bag tiffin charger bottle"): 1.0})
    s = M(text).score(found("red leather bag tiffin charger bottle"), lost("red leather bag with tiffin charger bottle"))
    raw = sum(e["bits"] for e in s["evidence"] if e["group"] == "description")
    assert raw > CAPS["description"][1] and s["groups"]["description"] == CAPS["description"][1]


# ── the gates ──────────────────────────────────────────────────────────────────
def _scored(bits, identifying=("brand",), conflicts=()):
    return {"bits": bits, "final": probability(bits), "identifying": list(identifying),
            "conflicts": list(conflicts), "blockers": []}


def test_gate_needs_identifying_no_conflict_and_a_clear_lead():
    clear = [_scored(8), _scored(5)]
    gate(clear)
    assert clear[0]["autoEligible"] and not clear[1]["autoEligible"] and clear[0]["margin"] == 3
    close = [_scored(8), _scored(8 - AUTO_MARGIN_BITS + 0.1)]
    gate(close)
    assert not close[0]["autoEligible"]
    generic = [_scored(8, identifying=())]
    gate(generic)
    assert not generic[0]["autoEligible"]
    contradiction = [_scored(8, conflicts=("colour_conflict",))]
    gate(contradiction)
    assert not contradiction[0]["autoEligible"]
    contested = [_scored(8)]
    gate(contested, contested=lambda s: True)
    assert not contested[0]["autoEligible"] and "another report" in contested[0]["blockers"][-1]


def test_only_leading_contenders_reach_the_review_queue():
    block = [_scored(6, ()), _scored(5.5, ()), _scored(5.2, ()), _scored(2, ())]
    gate(block)
    assert [s["reviewEligible"] for s in block] == [True, True, True, False]   # 3rd is within a bit of the top
    assert route(block[3], 0.85, 0.6) in ("reject", None)


def test_route_thresholds():
    s = _scored(10)
    gate([s])
    assert route(s, 0.85, 0.6) == "auto"
    assert route({**s, "autoEligible": False}, 0.85, 0.6) == "review"
    assert route({"final": 0.5, "reviewEligible": True}, 0.85, 0.6) == "reject"
    assert route({"final": POSSIBLE_MIN - 0.01}, 0.85, 0.6) is None


def test_mutual_best_detects_a_duplicate_report():
    m = M()
    item = found("navy backpack with a charger", trip_id="T5")
    a = lost("navy blue backpack with a charger", trip_id="T5", request_id="a")
    b = lost("navy blue backpack, charger inside", trip_id="T5", request_id="b")
    assert is_contested(m, item, a, [b], anchor_is_found=True)
    assert not is_contested(m, item, a, [lost("red umbrella", request_id="c")], anchor_is_found=True)


# ── robustness ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("desc", [
    None, "", "   ", "🎒🎒🎒 black bag 💼", "<script>alert(1)</script> bag", "a" * 20000, "{'$gt': ''}",
    "ब्लैक बैग", "１２３４５６ ＡＢＣ", "\x00\x01 bag",
], ids=lambda v: repr(v)[:24])
def test_odd_input_never_crashes(desc):
    s = M(WordOverlap()).score(found(desc if isinstance(desc, str) else ""), lost(desc))
    assert 0 < s["final"] <= 1 and math.isfinite(s["bits"])


def test_non_string_fields_are_tolerated():
    s = M().score(found(123, trip_id=5, bus_id=None, found_seat_area="front"),
                  lost(None, trip_id=5, seat_no="window", name=None, phone=None))
    assert 0 < s["final"] < 1


def test_scores_do_not_depend_on_candidate_order():
    m = M(WordOverlap())
    lo = cases.LOST[0]
    cands = cases.found_candidates(lo)
    first = {f["found_id"]: s["bits"] for f, s in m.score_block(lo, cands, query_is_found=False)}
    shuffled = list(cands)
    random.Random(7).shuffle(shuffled)
    again = {f["found_id"]: s["bits"] for f, s in m.score_block(lo, shuffled, query_is_found=False)}
    assert first == again


def test_randomised_pairs_respect_the_invariants():
    rng = random.Random(2026)
    words = ["black", "red", "blue", "bag", "phone", "wallet", "Wildcraft", "Skybags", "tiffin", "charger",
             "umbrella", "karuppu", "pai", "5CG1234XYZ", "ARUN", "laptop", "Dell", "keys", "file", "brown"]
    m = M(WordOverlap())
    for _ in range(400):
        fd = found(" ".join(rng.choices(words, k=rng.randint(0, 8))), trip_id=rng.choice([None, "T1", "T2"]),
                   bus_id=rng.choice([None, "B1", "B2"]), category=rng.choice([None, "bag", "phone", "other"]),
                   date=rng.choice(["2026-11-01", "2026-11-03", "2026-10-25", None]),
                   ocr_text=rng.choice(["", "ARUN KUMAR", "WILDCRAFT", "5CG1234XYZ"]))
        lo = lost(" ".join(rng.choices(words, k=rng.randint(0, 8))), trip_id=rng.choice([None, "T1", "T2"]),
                  bus_id=rng.choice([None, "B1", "B2"]), category=rng.choice([None, "bag", "wallet", "laptop"]))
        s = m.score(fd, lo)
        assert 0 <= s["final"] <= 1 and math.isfinite(s["bits"])
        for g, v in s["groups"].items():
            assert CAPS[g][0] <= v <= CAPS[g][1]
        assert s["bits"] == pytest.approx(PRIOR_BITS + sum(s["groups"].values()), abs=0.02)
        gate([s])
        if s["conflicts"] or not s["identifying"]:
            assert not s["autoEligible"] and route(s, 0.85, 0.6) != "auto"
        assert summary(s) == [e["note"] for e in s["evidence"]
                              if abs(e["bits"]) >= 1 or e["kind"] == "identifying"][:5]


def test_probability_is_monotonic_and_bounded():
    ps = [probability(b) for b in range(-20, 21)]
    assert ps == sorted(ps) and 0 < ps[0] < ps[-1] < 1 and probability(0) == 0.5


# ── the benchmark (deterministic model) ────────────────────────────────────────
@functools.lru_cache(maxsize=1)
def _v2_decisions():
    from match_benchmark import simulate
    return simulate(2, WordOverlap())

def test_benchmark_v2_never_auto_notifies_the_wrong_passenger():
    v1, v2 = metrics(1, WordOverlap()), metrics(2, WordOverlap())
    # v1 auto-notified dozens of wrong passengers (same trip was "proof").
    assert v1["auto_wrong"] >= 30 and v1["r_at_1"] < 0.6
    assert v2["auto_wrong"] == 0 and v2["fanr"] == 0
    assert v2["auto_ok"] >= 15                                   # most true pairs still go straight out
    assert v2["surfaced"] == f"{sum(1 for lo in cases.LOST if lo['truth'])}/{sum(1 for lo in cases.LOST if lo['truth'])}"
    assert v2["r_at_1"] == 1.0 and v2["mrr"] == 1.0              # the true item always ranks first
    assert v2["review_junk"] <= v1["review_junk"] + v1["auto_wrong"]


@pytest.mark.parametrize("rid", ["l-red-vs-green", "l-skybags-conflict", "l-vague", "l-other-bus",
                                 "l-samsung-phone"])
def test_benchmark_reports_whose_item_was_never_found_get_no_auto_match(rid):
    decisions = _v2_decisions()
    assert not any(k[0] == rid and d == "auto" for k, d in decisions.items())


@pytest.mark.parametrize("rid", ["l-navy", "l-navy-dup", "l-umbrella"])
def test_benchmark_ambiguous_reports_go_to_staff_not_straight_out(rid):
    decisions = _v2_decisions()
    lo = next(x for x in cases.LOST if x["request_id"] == rid)
    assert decisions.get((rid, lo["truth"])) == "review"


# ── colour shares (camera scans) ───────────────────────────────────────────────
def _scan(*colours):
    return found("A laptop. Colours: " + ", ".join(f"{c} {int(s * 100)}%" for c, s in colours) + ".",
                 category="laptop", captureMode="camera_scan",
                 attributes={"category": {"value": "laptop", "confidence": 0.97},
                             "colours": [{"name": c, "share": s} for c, s in colours]})


def test_a_small_patch_of_colour_counts_little():
    """A white laptop with a 12% yellow speck must not be 'both yellow' at full strength."""
    item = _scan(("white", 0.49), ("brown", 0.19), ("yellow", 0.12))
    speck = signals(M().score(item, lost("yellow laptop", category="laptop")))
    main = signals(M().score(item, lost("white laptop", category="laptop")))
    assert speck["colour"] < 1.5 < main["colour"]


def test_missing_the_main_colour_counts_against():
    item = _scan(("black", 0.7), ("red", 0.2))
    s = signals(M().score(item, lost("red bag", category="laptop")))
    assert s["colour_main"] == -1.0


def test_scan_notes_do_not_double_count_colours():
    """The scan's notes repeat its colours as words; only the measured shares count."""
    item = _scan(("white", 0.9), ("yellow", 0.09))
    assert signals(M().score(item, lost("yellow laptop", category="laptop")))["colour"] < 1.0
