"""Phase 2 shared vocabulary: readiness slots, colour maths and plausible distractors."""
import pytest

from vocabulary import (COLOUR_LAB, COLOUR_RGB, delta_e2000, detail_slots, nearest_colour, rgb_to_lab,
                        similar_colours, vocabulary_payload)


@pytest.mark.parametrize("text,expected", [
    ("black bag", {"colour"}),
    ("Navy Wildcraft backpack", {"colour", "brand"}),
    ("bag with a tiffin box and charger", {"contents"}),
    ("has a CSK sticker on the front", {"marks"}),
    ("Red American Tourister trolley, tiffin inside, name tag says Arun", {"colour", "brand", "contents", "marks"}),
    ("கருப்பு பை, உள்ளே சாவி இருக்கு", {"colour", "contents"}),        # Tamil: black bag, keys inside
    ("karuppu bag, ulle tiffin", {"colour", "contents"}),               # Tanglish
    ("leather bag", {"colour"}),                                        # material counts with colour
    ("", set()),
])
def test_detail_slots(text, expected):
    slots = detail_slots(text)
    assert {k for k in ("colour", "brand", "contents", "marks") if slots[k]} == expected
    assert slots["score"] == 25 * len(expected)


def test_delta_e2000_reference_pair():
    # Sharma, Wu & Dalal (2005) test data, pair 1: ΔE00 = 2.0425.
    assert delta_e2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(2.0425, abs=1e-4)
    assert delta_e2000((50, 0, 0), (50, 0, 0)) == 0


def test_palette_colours_map_to_themselves():
    for name, rgb in COLOUR_RGB.items():
        assert nearest_colour(rgb_to_lab(rgb))[0] == name


def test_slightly_off_colours_still_map():
    assert nearest_colour(rgb_to_lab((150, 20, 30)))[0] == "red"        # maroon-ish
    assert nearest_colour(rgb_to_lab((20, 30, 90)))[0] in ("blue", "black")


def test_distractors_are_plausible_neighbours_not_opposites():
    near = similar_colours("red", 2)
    assert "red" not in near and len(near) == 2
    far = max(COLOUR_LAB, key=lambda c: delta_e2000(COLOUR_LAB["red"], COLOUR_LAB[c]))
    assert far not in near


def test_payload_shape():
    v = vocabulary_payload()
    assert {s["key"] for s in v["storage"]} == {"seat", "overhead", "under_seat", "boot", "front_cabin"}
    assert "american tourister" in v["brands"]


# ── item-specific detail prompts (the report form's bubbles) ─────────────────
from vocabulary import CATEGORY_LABELS, detail_prompts  # noqa: E402


@pytest.mark.parametrize("category", sorted(CATEGORY_LABELS))
def test_every_item_kind_has_its_own_prompts(category):
    prompts = detail_prompts(category)
    keys = [p["key"] for p in prompts]
    assert len(prompts) >= 4 and len(set(keys)) == len(keys) and any(p["core"] for p in prompts)
    assert all(p["chip"] and p["insert"] and "{" not in p["chip"] for p in prompts)


def test_prompts_suit_the_item():
    keys = {c: {p["key"] for p in detail_prompts(c)} for c in CATEGORY_LABELS}
    assert "contents" not in keys["phone"] and "contents" not in keys["umbrella"]   # nothing "inside" a phone
    assert {"case", "serial", "wallpaper"} <= keys["phone"] and {"type", "handle"} <= keys["umbrella"]
    assert "name" in keys["documents"] and "zip" in keys["bag"]
    assert "Wildcraft" in detail_prompts("bag")[0]["chip"]                        # brand examples of that kind


@pytest.mark.parametrize("category,text,done,score", [
    ("umbrella", "Black folding umbrella, curved wooden handle, small tear", {"colour", "type", "handle", "marks"}, 100),
    ("phone", "Samsung phone in a blue cover, cracked corner, IMEI 356789104455667",
     {"brand", "case", "colour", "marks", "serial"}, 100),
    ("phone", "my phone", set(), 0),
    ("documents", "Blue folder with my certificates, name Priya Raman", {"name", "contents", "folder"}, 100),
    ("bag", "Brand: Pesoma", {"brand"}, 25),                                         # an unknown brand, labelled
])
def test_prompt_detection_and_score(category, text, done, score):
    slots = detail_slots(text, category)
    assert {k for k, v in slots.items() if k != "score" and v} >= done and slots["score"] == score
