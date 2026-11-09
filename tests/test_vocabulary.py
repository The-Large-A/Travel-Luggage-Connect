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
