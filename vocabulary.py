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
            "Adidas", "F Gear", "Lavie", "Caprese", "Samsonite", "Eastpak", "Quechua", "Rimowa", "Coach",
            "Tommy Hilfiger"),
    "wallet": ("Woodland", "Tommy Hilfiger", "Allen Solly", "Hidesign", "Lavie", "Caprese", "Baggit", "Prada",
               "Coach"),
    "phone": ("Samsung", "Apple", "Redmi", "Xiaomi", "Vivo", "Oppo", "Realme", "OnePlus", "Motorola", "Nokia",
              "Sony", "Microsoft", "Google"),
    "laptop": ("Dell", "HP", "Lenovo", "Asus", "Acer", "Apple", "MSI", "Microsoft"),
    "spectacles": ("Lenskart", "Titan", "Ray-Ban", "Vincent Chase", "Fastrack"),
    "umbrella": ("Popy", "John's", "Citizen", "Sun Brand"),
    "clothing": ("Raymond", "Peter England", "Allen Solly", "Louis Philippe", "Van Heusen", "Pothys", "RmKV",
                 "Puma", "Nike", "Adidas"),
    "documents": (),
    "other": ("Milton", "Cello", "Tupperware", "Borosil"),
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


# ── what the item IS, from free text ─────────────────────────────────────────
# Canonical tokens (after claims.normalize folding) that name the item itself.
# The FIRST such noun in a description is the item; later ones are contents
# ("black bag with a phone inside" is a bag).
CATEGORY_NOUNS = {
    "bag": ("bag", "bags", "backpack", "suitcase", "trolley", "handbag", "luggage", "satchel", "sling",
            "duffel", "rucksack", "schoolbag", "haversack", "tote", "briefcase", "pai", "பை", "laptopbag"),
    "wallet": ("wallet",),
    "phone": ("phone",),
    "laptop": ("laptop", "tablet", "ipad", "macbook"),
    "spectacles": ("spectacles", "sunglasses", "goggles"),
    "umbrella": ("umbrella",),
    "documents": ("documents", "document", "file", "files", "folder", "papers", "envelope", "passport",
                  "certificate"),
    "clothing": ("clothes", "jacket", "sweater", "shawl", "jerkin", "coat", "cap", "hat", "hoodie", "blazer",
                 "kurta", "dupatta", "veshti", "lungi"),
}
NOUN_CATEGORY = {noun: cat for cat, nouns in CATEGORY_NOUNS.items() for noun in nouns}

# Categories a passenger and a depot can reasonably file the same thing under
# ("purse" is a wallet to some and a handbag to others; a laptop bag is a bag).
COMPATIBLE_CATEGORIES = frozenset(frozenset(p) for p in (
    ("bag", "wallet"), ("bag", "laptop"), ("laptop", "phone"), ("documents", "wallet"), ("bag", "clothing")))

# Brand → the categories it makes (lower-cased keys).
BRAND_HOMES = {}
for _cat, _names in BRANDS.items():
    for _b in _names:
        BRAND_HOMES.setdefault(_b.casefold(), set()).add(_cat)
BRAND_HOMES = {b: frozenset(c) for b, c in BRAND_HOMES.items()}

# What can sensibly be INSIDE each kind of item: the pool for "name one thing
# inside it" options. A wallet never offers "umbrella"; a phone has no contents.
CONTENT_POOLS = {
    "bag": ("clothes", "tiffin", "bottle", "book", "charger", "towel", "medicine", "earphones", "keys",
            "umbrella", "spectacles", "wallet", "powerbank", "money"),
    "wallet": ("money", "card", "photo", "ticket", "bill", "keys"),
    "documents": ("certificate", "passport", "card", "photo", "bill", "ticket", "book"),
    "clothing": ("money", "keys", "ticket", "bill", "card", "earphones"),
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


# Common shades of each canonical colour, so a camera reading of a navy bag is
# named "blue" (the word an owner uses) rather than whichever single reference
# colour happens to be nearest — with one prototype per name, dark navy fell
# closer to purple. Shades are named the way passengers describe them.
COLOUR_SHADES = {
    "blue": [(24, 34, 74), (30, 52, 110), (20, 110, 130), (90, 140, 200),     # navy, dark, teal, sky
             (62, 76, 98)],                                                     # slate / washed-out navy
    "red": [(110, 24, 36), (160, 30, 40)],                                       # maroon, crimson
    "brown": [(190, 160, 115), (150, 110, 70), (80, 55, 40)],                    # khaki/beige, tan, dark brown
    "green": [(100, 110, 50), (30, 80, 50)],                                     # olive, bottle green
    "white": [(235, 228, 205)],                                                  # cream / off-white
    "grey": [(60, 62, 66), (170, 170, 172)],                                     # charcoal, light grey
    "pink": [(200, 70, 120)],                                                    # magenta
    "purple": [(80, 40, 110), (170, 140, 200)],                                  # deep purple, lavender
    "yellow": [(220, 190, 90)],                                                  # mustard
}
_SHADE_LAB = [(name, lab) for name, lab in COLOUR_LAB.items()] + \
    [(name, rgb_to_lab(rgb)) for name, shades in COLOUR_SHADES.items() for rgb in shades]


def nearest_colour(lab) -> tuple:
    """(canonical colour name, ΔE2000 distance) closest to a Lab colour, over all named shades."""
    return min(((name, delta_e2000(lab, ref)) for name, ref in _SHADE_LAB), key=lambda t: t[1])


def similar_colours(name: str, n: int = 2) -> list:
    """The n palette colours perceptually closest to `name` — plausible distractors."""
    ref = COLOUR_LAB[name]
    others = sorted((delta_e2000(ref, lab), other) for other, lab in COLOUR_LAB.items() if other != name)
    return [other for _, other in others[:n]]



# ── colour confusion (shared by the matcher and the ownership questions) ────
# Pairs people call by either name, or that bus lighting / phone cameras swap:
# the six palette pairs under ΔE00 25 plus the common naming confusions
# (charcoal: black/grey, navy: black/blue, tan: brown/orange, maroon: red/brown,
# cream: yellow/white, teal: blue/green, olive: green/grey, magenta: pink/purple,
# slate: blue/grey).
COLOUR_ADJACENT = frozenset(frozenset(p) for p in (
    ("black", "grey"), ("black", "blue"), ("black", "brown"), ("grey", "silver"), ("grey", "white"),
    ("silver", "white"), ("red", "pink"), ("red", "orange"), ("red", "brown"), ("orange", "brown"),
    ("orange", "gold"), ("orange", "yellow"), ("yellow", "gold"), ("yellow", "white"), ("gold", "brown"),
    ("pink", "purple"), ("purple", "blue"), ("blue", "green"), ("green", "grey"), ("silver", "gold"),
    ("blue", "grey"),                     # slate: a navy bag under bus lighting reads as either
))

# How common each colour is among lost bags and accessories (sums to 1). A match
# on a rare colour is stronger evidence than a match on black (term frequency).
COLOUR_PRIOR = {"black": 0.32, "blue": 0.14, "grey": 0.09, "brown": 0.08, "red": 0.06, "white": 0.05,
                "green": 0.05, "pink": 0.04, "silver": 0.04, "gold": 0.04, "purple": 0.03, "orange": 0.03,
                "yellow": 0.03}
assert set(COLOUR_PRIOR) == set(COLOURS) and abs(sum(COLOUR_PRIOR.values()) - 1) < 1e-9


def colours_confusable(a: str, b: str) -> bool:
    return a == b or frozenset((a, b)) in COLOUR_ADJACENT


def contrasting_colours(true_colour: str, n: int = 3, min_delta: float = 25.0) -> list:
    """
    Up to n colours an owner can never confuse with `true_colour` or with each
    other (no confusable pair, CIEDE2000 >= min_delta between all of them),
    chosen to be about as common as the true colour, so popularity does not
    give the answer away (flat options). Deterministic.
    """
    if true_colour not in COLOUR_LAB:
        return []
    chosen = [true_colour]
    target = COLOUR_PRIOR[true_colour]
    # An owner who half-remembers a neighbouring shade (charcoal for black) should
    # still find the true colour the closest option, so options next to one of the
    # true colour's own neighbours are mildly penalised (a soft rule: a strict one
    # leaves too few colours and wrecks flatness).
    near = {c for c in COLOUR_LAB if c != true_colour and colours_confusable(c, true_colour)}

    def cost(c):
        second_order = any(colours_confusable(c, nb) for nb in near)
        return (abs(COLOUR_PRIOR[c] - target) + (0.03 if second_order else 0.0), c)
    for cand in sorted(COLOUR_LAB, key=cost):
        if cand in chosen:
            continue
        if any(colours_confusable(cand, c) or delta_e2000(COLOUR_LAB[cand], COLOUR_LAB[c]) < min_delta
               for c in chosen):
            continue
        chosen.append(cand)
        if len(chosen) == n + 1:
            break
    return chosen[1:]


def option_flatness(options, prior=None) -> float:
    """Best blind-guess success (max prior / sum of priors) over a set of options."""
    prior = prior or COLOUR_PRIOR
    weights = [prior.get(o, min(prior.values())) for o in options]
    return max(weights) / sum(weights) if weights else 1.0

# ── description detail (the passenger "match readiness" meter) ───────────────
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_BRAND_TOKENS = {normalize_text(b) for b in ALL_BRANDS}
_MATERIAL_SET = frozenset(MATERIALS)
_MARK_SET = frozenset(unicodedata.normalize("NFC", w).casefold() for w in MARK_WORDS)
_CONTENT_SET = frozenset(CONTENTS)
_COLOUR_SET = frozenset(COLOURS)

SLOTS = ("colour", "brand", "contents", "marks")


# ── item-specific detail prompts (the clickable bubbles on the report form) ──
# Per kind of item, the details that best tell one item from another, the ones
# that IDENTIFY first (brand, a name, a serial, what was inside), because those
# are what let the matcher notify the owner without waiting for staff.
# Each prompt: key, chip text, the words inserted on click, how to tell it is
# already covered ("colour" | "brand" | "contents" | "marks" | "digits" | a word
# list), and whether it counts towards the readiness score ("core").
# {brands} becomes this kind of item's own brand examples.
DETAIL_PROMPTS = {
    "bag": [
        ("brand", "Brand ({brands})", "Brand: ", "brand", True),
        ("contents", "What was inside?", "Inside: ", "contents", True),
        ("colour", "Colour / material", "Colour: ", "colour", True),
        ("marks", "Sticker, keychain or patch?", "Marks: ", "marks", True),
        ("name", "Name or initials on a tag?", "Name on tag: ", ("tag", "nametag", "initials", "name", "written"), False),
        ("zip", "Zip pull / tag colour", "Zip pull colour: ", ("zip", "zipper", "pull", "chain"), False),
        ("type", "Backpack, trolley, sling or handbag?", "Type: ", ("backpack", "trolley", "suitcase", "sling", "handbag",
                                                                    "duffel", "tote", "briefcase", "schoolbag"), False),
        ("size", "Size (small / cabin / large, litres)", "Size: ", ("small", "medium", "large", "cabin", "litre",
                                                                     "litres", "big"), False),
    ],
    "wallet": [
        ("brand", "Brand ({brands})", "Brand: ", "brand", True),
        ("contents", "Cards, cash or a photo inside? (no card numbers)", "Inside: ", "contents", True),
        ("colour", "Colour / leather or cloth", "Colour: ", "colour", True),
        ("marks", "Initials, a mark or worn corner?", "Marks: ", "marks", True),
        ("type", "Folding, long or zip purse?", "Type: ", ("bifold", "trifold", "folding", "long", "zip", "zipper",
                                                           "clutch", "pouch"), False),
    ],
    "phone": [
        ("brand", "Brand & model ({brands})", "Phone: ", "brand", True),
        ("case", "Case or cover colour", "Case: ", ("case", "cover", "pouch", "flip", "bumper"), True),
        ("colour", "Phone colour", "Phone colour: ", "colour", True),
        ("marks", "Cracks, stickers or a scratch?", "Marks: ", ("crack", "cracked", "broken", "sticker", "scratch",
                                                                 "scratches", "dent", "keeral"), True),
        ("wallpaper", "Lock-screen picture", "Wallpaper: ", ("wallpaper", "lockscreen", "lock", "screen"), False),
        ("serial", "IMEI (from the box or bill)", "IMEI: ", "digits", False),
    ],
    "laptop": [
        ("brand", "Brand & model ({brands})", "Laptop: ", "brand", True),
        ("colour", "Colour", "Colour: ", "colour", True),
        ("marks", "Stickers or marks on the lid?", "Lid: ", ("sticker", "stickers", "scratch", "dent", "mark", "lid"), True),
        ("sleeve", "Sleeve, cover or bag?", "Sleeve: ", ("sleeve", "cover", "case", "bag", "pouch"), True),
        ("serial", "Serial number (from the box or bill)", "Serial: ", "digits", False),
        ("charger", "Charger or mouse with it?", "With it: ", ("charger", "adapter", "mouse", "cable"), False),
        ("wallpaper", "Login-screen picture", "Wallpaper: ", ("wallpaper", "lockscreen", "screen", "background"), False),
    ],
    "spectacles": [
        ("type", "Power glasses or sunglasses?", "Type: ", ("power", "reading", "sunglasses", "sun", "goggles",
                                                            "cooling", "computer", "lens", "lenses"), True),
        ("colour", "Frame colour", "Frame: ", "colour", True),
        ("case", "Case colour or type", "Case: ", ("case", "box", "pouch", "cover"), True),
        ("brand", "Brand ({brands})", "Brand: ", "brand", False),
        ("marks", "A repair, scratch or bent arm?", "Marks: ", ("scratch", "bent", "broken", "tape", "repair", "screw"), False),
    ],
    "umbrella": [
        ("colour", "Colour / pattern", "Colour: ", "colour", True),
        ("type", "Folding or long?", "Type: ", ("folding", "fold", "foldable", "long", "stick", "compact", "automatic",
                                                "auto"), True),
        ("handle", "Handle colour and shape", "Handle: ", ("handle", "hook", "curved", "straight", "grip"), True),
        ("marks", "A tear, patch or name?", "Marks: ", "marks", True),
        ("brand", "Brand ({brands})", "Brand: ", "brand", False),
        ("cover", "Cover / sleeve with it?", "Cover: ", ("cover", "sleeve", "pouch", "case"), False),
    ],
    "documents": [
        ("name", "Whose name is on them?", "Name on documents: ", ("name", "written", "my"), True),
        ("contents", "Which documents? (certificates, bills…)", "Documents: ", "contents", True),
        ("folder", "Folder or cover colour", "Folder: ", ("folder", "file", "cover", "envelope", "plastic"), True),
        ("count", "How many papers or files?", "How many: ", "digits_or_count", False),
    ],
    "clothing": [
        ("type", "What exactly? (shirt, jacket, shawl…)", "Item: ", ("shirt", "jacket", "sweater", "shawl", "saree",
                                                                     "dress", "cap", "hat", "hoodie", "coat", "kurta",
                                                                     "dupatta", "veshti", "lungi", "jerkin", "clothes"), True),
        ("colour", "Colour / pattern", "Colour: ", "colour", True),
        ("brand", "Brand label ({brands})", "Label: ", "brand", True),
        ("size", "Size (S / M / L / XL)", "Size: ", ("size", "small", "medium", "large", "xl", "xxl", "s", "m", "l"), False),
        ("pockets", "Anything in the pockets?", "In the pockets: ", "contents", False),
        ("marks", "A stain, tear or name label?", "Marks: ", "marks", False),
    ],
    "other": [
        ("what", "What exactly is it?", "It is a ", "anything", True),
        ("colour", "Colour / material", "Colour: ", "colour", True),
        ("marks", "A sticker, mark or name on it?", "Marks: ", "marks", True),
        ("brand", "Brand", "Brand: ", "brand", False),
    ],
}
assert set(DETAIL_PROMPTS) == set(CATEGORY_LABELS)


def _prompt_done(kind, text, toks, words, norm) -> bool:
    if kind == "colour":
        return bool(toks & _COLOUR_SET or words & _MATERIAL_SET)
    if kind == "brand":
        return any(re.search(rf"(?<!\w){re.escape(b)}(?!\w)", norm) for b in _BRAND_TOKENS if b)
    if kind == "contents":
        return bool(toks & _CONTENT_SET)
    if kind == "marks":
        return bool(words & _MARK_SET or toks & {"sticker", "scratch"})
    if kind == "digits":
        return bool(re.search(r"\d{5,}", re.sub(r"[\s-]", "", text or "")))
    if kind == "digits_or_count":
        return bool(re.search(r"\d", text or "")) or bool(words & {"one", "two", "three", "few", "many", "several"})
    if kind == "anything":
        return len(words) >= 3
    return bool(words & set(kind) or toks & set(kind))


def _label_filled(insert, norm) -> bool:
    """'Brand: Pesoma' answers the brand prompt even for a brand we have never heard of."""
    label = normalize_text(insert)
    return bool(label) and bool(re.search(rf"(?<!\w){re.escape(label)} \S", norm))


def detail_prompts(category) -> list:
    """The bubbles for one kind of item, with its own brand examples filled in."""
    category = category if category in DETAIL_PROMPTS else "other"
    examples = ", ".join(BRANDS.get(category, ())[:3]) or "if any"
    return [{"key": k, "chip": chip.format(brands=examples), "insert": insert, "core": core,
             "match": kind if isinstance(kind, str) else list(kind)}
            for k, chip, insert, kind, core in DETAIL_PROMPTS[category]]


def detail_slots(text, category=None) -> dict:
    """
    Which details a description covers. With a category: that item's own prompts,
    scored over its core ones (a phone is never asked what was inside it). Without:
    the original four slots (colour/material, brand, contents, marks), 25 each.
    Server-side twin of the meter in static/js/report-form.js.
    """
    norm = normalize_text(text or "")
    toks = set(tokens(text or ""))
    words = set(norm.split())
    if category in DETAIL_PROMPTS:
        found = {k: _prompt_done(kind, text, toks, words, norm) or _label_filled(insert, norm)
                 for k, _c, insert, kind, _core in DETAIL_PROMPTS[category]}
        core = [k for k, _c, _i, _kind, is_core in DETAIL_PROMPTS[category] if is_core]
        found["score"] = round(100 * sum(found[k] for k in core) / len(core))
        return found
    found = {s: _prompt_done(s, text, toks, words, norm) for s in SLOTS}
    found["score"] = 25 * sum(found[s] for s in SLOTS)
    return found


# ── description strength (the live meter on the passenger AND depot forms) ───
# Five factors, equal weight, scored over the ones that apply to the item (a
# phone has no "contents"). Server twin of static/js/description_meter.js.
FEATURE_WORDS = ("zip", "zipper", "strap", "straps", "handle", "wheel", "wheels", "pocket", "pockets", "keychain",
                 "sticker", "scratch", "tag", "patch", "logo", "buckle", "lock", "chain", "mark", "marks", "stain",
                 "tear", "torn", "crack", "cracked", "ribbon", "initials", "embroidered", "badge", "dent", "case",
                 "cover", "hook", "frame", "keeral")
STRENGTH_FACTORS = ("type", "colour", "identifier", "contents", "features")
NO_CONTENTS = frozenset({"phone", "laptop", "spectacles", "umbrella"})
STRENGTH_BANDS = ((30, "weak"), (60, "moderate"), (85, "strong"), (100, "excellent"))


def description_strength(text, category=None) -> dict:
    """{factor: bool, ..., "score": 0-100, "band": weak|moderate|strong|excellent, "missing": [...]}"""
    norm = normalize_text(text or "")
    toks = set(tokens(text or ""))
    words = set(norm.split())
    nouns = {n for ns in CATEGORY_NOUNS.values() for n in ns}
    found = {
        "type": bool(toks & nouns or words & nouns),
        "colour": bool(toks & _COLOUR_SET),
        "identifier": (any(re.search(rf"(?<!\w){re.escape(b)}(?!\w)", norm) for b in _BRAND_TOKENS if b)
                       or bool(re.search(r"\d{5,}", re.sub(r"[\s-]", "", text or "")))
                       or bool(re.search(r"(?<![a-z0-9])(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{5,}", norm))
                       or _label_filled("Brand:", norm) or bool(words & {"name", "initials", "nametag"})),
        "contents": bool(toks & _CONTENT_SET),
        "features": bool(words & set(FEATURE_WORDS) or words & _MARK_SET or toks & {"sticker", "scratch"}),
    }
    applicable = [f for f in STRENGTH_FACTORS if not (f == "contents" and category in NO_CONTENTS)]
    score = round(100 * sum(found[f] for f in applicable) / len(applicable))
    band = next(name for top, name in STRENGTH_BANDS if score <= top)
    return {**found, "score": score, "band": band, "missing": [f for f in applicable if not found[f]],
            "applicable": applicable}


def vocabulary_payload() -> dict:
    """JSON for the browser: the same word lists the server uses."""
    def variants(table, keys):
        return {k: sorted({unicodedata.normalize("NFC", v).casefold() for v in table[k]} | {k}) for k in keys}
    return {
        "categories": [{"key": k, "label": CATEGORY_LABELS[k][0], "tamil": CATEGORY_LABELS[k][1],
                        "hints": list(CATEGORY_HINTS.get(k, ())), "prompts": detail_prompts(k)} for k in CATEGORIES],
        "storage": [{"key": k, "label": label, "tamil": ta} for k, label, ta in STORAGE_LOCATIONS],
        "colours": variants(COLOURS, COLOURS),
        "materials": list(MATERIALS),
        "brands": [b.casefold() for b in ALL_BRANDS],
        "contents": variants(OBJECTS, CONTENTS),
        "marks": sorted(_MARK_SET),
        # item nouns plus every spelling the server folds into them ("mobile" → phone)
        "itemNouns": sorted({n for ns in CATEGORY_NOUNS.values() for n in ns} |
                            {unicodedata.normalize("NFC", v).casefold() for k in CATEGORY_NOUNS.get("wallet", ()) +
                             ("phone", "spectacles", "umbrella", "clothes", "laptop") for v in OBJECTS.get(k, ())}),
        "features": list(FEATURE_WORDS),
        "noContents": sorted(NO_CONTENTS),
    }
