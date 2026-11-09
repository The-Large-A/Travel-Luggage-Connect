"""
Shared item vocabulary (Phase 2): one controlled list of categories, colours,
materials, external features, brands, contents and storage locations, used by

  * the passenger report form (category dropdown, readiness meter, storage pills),
  * the depot smart-intake scan (CLIP zero-shot labels, named colours),
  * ownership questions (plausible multiple-choice distractors).

The field structure follows the IATA bag-description model (type, colour,
material, external features) without claiming IATA's own (paywalled) codes.
Colour and object synonyms (English / Tamil / Tanglish) come from
claims.normalize so answers, descriptions and questions fold the same way.
"""
import math
import re
import unicodedata

from claims.attributes import CATEGORIES
from claims.normalize import COLOURS, OBJECTS, normalize_text, tokens

# Reference sRGB for each canonical colour (used for named-colour lookup in Lab
# space and for choosing the closest — i.e. most plausible — distractor colours).
COLOUR_RGB = {
    "red": (196, 32, 38), "blue": (36, 72, 168), "green": (40, 140, 64), "yellow": (236, 200, 40),
    "black": (24, 24, 26), "white": (242, 242, 240), "grey": (128, 128, 130), "brown": (118, 76, 40),
    "orange": (236, 122, 30), "pink": (234, 128, 170), "purple": (118, 58, 150),
    "silver": (192, 192, 200), "gold": (204, 160, 56),
}
assert set(COLOUR_RGB) == set(COLOURS)

MATERIALS = ("leather", "nylon", "polyester", "canvas", "denim", "jute", "plastic", "metal", "rexine", "cloth")

EXTERNAL_FEATURES = ("wheels", "telescopic handle", "shoulder strap", "carry handle", "side pockets",
                     "laptop sleeve", "buckle", "padlock", "keychain", "name tag", "sticker", "patch")

BRANDS = {
    "bag": ("Wildcraft", "American Tourister", "Skybags", "VIP", "Safari", "Aristocrat", "Puma", "Nike",
            "Adidas", "F Gear", "Lavie", "Caprese", "Samsonite"),
    "wallet": ("Woodland", "Tommy Hilfiger", "Allen Solly", "Hidesign", "Lavie", "Caprese", "Baggit"),
    "phone": ("Samsung", "Apple", "Redmi", "Xiaomi", "Vivo", "Oppo", "Realme", "OnePlus", "Motorola", "Nokia"),
    "laptop": ("Dell", "HP", "Lenovo", "Asus", "Acer", "Apple", "MSI"),
    "spectacles": ("Lenskart", "Titan", "Ray-Ban", "Vincent Chase", "Fastrack"),
    "umbrella": ("Popy", "John's", "Citizen", "Sun Brand"),
    "clothing": ("Raymond", "Peter England", "Allen Solly", "Louis Philippe", "Van Heusen", "Pothys", "RmKV"),
    "documents": (),
    "other": (),
}
ALL_BRANDS = tuple(sorted({b for bs in BRANDS.values() for b in bs}, key=str.lower))

# Contents a passenger may mention, as the canonical object words of claims.normalize.
CONTENTS = tuple(k for k in OBJECTS if k not in ("sticker", "scratch"))

# Words that describe a distinguishing mark (English / Tanglish / Tamil).
MARK_WORDS = ("sticker", "scratch", "tag", "nametag", "keychain", "badge", "patch", "logo", "initials",
              "torn", "tear", "stain", "broken", "label", "name", "written", "embroidered", "dent",
              "keeral", "கீறல்", "ஸ்டிக்கர்", "பெயர்")

STORAGE_LOCATIONS = (
    ("seat", "At my seat", "இருக்கையில்"),
    ("overhead", "Overhead rack", "மேல் அடுக்கு"),
    ("under_seat", "Under the seat", "இருக்கையின் கீழ்"),
    ("boot", "Luggage boot", "பின்புற டிக்கி"),
    ("front_cabin", "Front / driver cabin", "முன் பகுதி"),
)
STORAGE_KEYS = frozenset(k for k, _, _ in STORAGE_LOCATIONS)

CATEGORY_LABELS = {
    "bag": ("Bag / backpack / suitcase", "பை"), "wallet": ("Wallet / purse", "பர்ஸ்"),
    "phone": ("Mobile phone", "கைபேசி"), "laptop": ("Laptop / tablet", "மடிக்கணினி"),
    "spectacles": ("Spectacles", "கண்ணாடி"), "umbrella": ("Umbrella", "குடை"),
    "documents": ("Documents / files", "ஆவணங்கள்"), "clothing": ("Clothing", "உடை"),
    "other": ("Something else", "மற்றவை"),
}
assert set(CATEGORY_LABELS) == set(CATEGORIES)

# Optional, non-confidential prompts shown under the description per category.
CATEGORY_HINTS = {
    "bag": ("What was inside?", "Any tag or keychain on the zip?"),
    "wallet": ("Any cards or photos inside?", "Colour of the inside lining?"),
    "phone": ("Case colour?", "What is on the lock screen?"),
    "laptop": ("Was the charger inside?", "Any stickers on the lid?"),
    "spectacles": ("Frame colour?", "Was there a case?"),
    "umbrella": ("Folding or long?", "Handle colour?"),
    "documents": ("What kind of documents?", "Was there a folder or cover?"),
    "clothing": ("What item of clothing?", "Any label or name inside?"),
    "other": ("What exactly is it?",),
}


# ── colour maths (Lab + CIEDE2000) ───────────────────────────────────────────
def _srgb_to_linear(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    """sRGB (0-255) → CIE L*a*b* (D65)."""
    r, g, b = (_srgb_to_linear(v) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.00000
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e2000(lab1, lab2) -> float:
    """CIEDE2000 colour difference (perceptual distance between two Lab colours)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cm ** 7 / (Cm ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))
    Lpm, Cpm = (L1 + L2) / 2, (C1p + C2p) / 2
    if C1p * C2p == 0:
        hpm = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hpm = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hpm = (h1p + h2p + 360) / 2
    else:
        hpm = (h1p + h2p - 360) / 2
    T = (1 - 0.17 * math.cos(math.radians(hpm - 30)) + 0.24 * math.cos(math.radians(2 * hpm))
         + 0.32 * math.cos(math.radians(3 * hpm + 6)) - 0.20 * math.cos(math.radians(4 * hpm - 63)))
    d_theta = 30 * math.exp(-(((hpm - 275) / 25) ** 2))
    Rc = 2 * math.sqrt(Cpm ** 7 / (Cpm ** 7 + 25 ** 7))
    Sl = 1 + (0.015 * (Lpm - 50) ** 2) / math.sqrt(20 + (Lpm - 50) ** 2)
    Sc, Sh = 1 + 0.045 * Cpm, 1 + 0.015 * Cpm * T
    Rt = -math.sin(math.radians(2 * d_theta)) * Rc
    return math.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


COLOUR_LAB = {name: rgb_to_lab(rgb) for name, rgb in COLOUR_RGB.items()}


def nearest_colour(lab) -> tuple:
    """(canonical colour name, ΔE2000 distance) closest to a Lab colour."""
    return min(((name, delta_e2000(lab, ref)) for name, ref in COLOUR_LAB.items()), key=lambda t: t[1])


def similar_colours(name: str, n: int = 2) -> list:
    """The n palette colours perceptually closest to `name` — plausible distractors."""
    ref = COLOUR_LAB[name]
    others = sorted((delta_e2000(ref, lab), other) for other, lab in COLOUR_LAB.items() if other != name)
    return [other for _, other in others[:n]]


# ── description detail (the passenger "match readiness" meter) ───────────────
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_BRAND_TOKENS = {normalize_text(b) for b in ALL_BRANDS}
_MATERIAL_SET = frozenset(MATERIALS)
_MARK_SET = frozenset(unicodedata.normalize("NFC", w).casefold() for w in MARK_WORDS)
_CONTENT_SET = frozenset(CONTENTS)
_COLOUR_SET = frozenset(COLOURS)

SLOTS = ("colour", "brand", "contents", "marks")


def detail_slots(text) -> dict:
    """
    Which of the four details a description covers: colour/material, brand,
    contents, distinguishing marks. Each slot is worth 25 points.
    Server-side twin of the client meter in static/js/report-form.js.
    """
    norm = normalize_text(text or "")
    toks = set(tokens(text or ""))
    words = set(norm.split())
    found = {
        "colour": bool(toks & _COLOUR_SET or words & _MATERIAL_SET),
        "brand": any(re.search(rf"(?<!\w){re.escape(b)}(?!\w)", norm) for b in _BRAND_TOKENS if b),
        "contents": bool(toks & _CONTENT_SET),
        "marks": bool(words & _MARK_SET or toks & {"sticker", "scratch"}),
    }
    found["score"] = 25 * sum(found[s] for s in SLOTS)
    return found


def vocabulary_payload() -> dict:
    """JSON for the browser: the same word lists the server uses."""
    def variants(table, keys):
        return {k: sorted({unicodedata.normalize("NFC", v).casefold() for v in table[k]} | {k}) for k in keys}
    return {
        "categories": [{"key": k, "label": CATEGORY_LABELS[k][0], "tamil": CATEGORY_LABELS[k][1],
                        "hints": list(CATEGORY_HINTS.get(k, ()))} for k in CATEGORIES],
        "storage": [{"key": k, "label": label, "tamil": ta} for k, label, ta in STORAGE_LOCATIONS],
        "colours": variants(COLOURS, COLOURS),
        "materials": list(MATERIALS),
        "brands": [b.casefold() for b in ALL_BRANDS],
        "contents": variants(OBJECTS, CONTENTS),
        "marks": sorted(_MARK_SET),
    }
