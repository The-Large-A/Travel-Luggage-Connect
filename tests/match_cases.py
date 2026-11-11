"""
Labelled matching benchmark: lost reports and found items on ONE busy route
over five days, so every block is crowded with plausible distractors.

Each lost report names the found item that is truly its own (`truth`), or
None when the item has not been handed in. Hard negatives are deliberate:
five black bags on one trip, same trip but a different item, same brand in a
different category, adjacent and contradictory colours, Tamil / Tanglish
descriptions, duplicate reports, a different bus, an item logged before the
journey, vague reports and items nobody reported.

Used by tests/test_matching.py (deterministic fake models) and
tools/match_benchmark.py (the real SBERT model, old vs new matcher).
"""
ROUTE = "R1"


def _scan(cat, conf, colours, contents=(), brand=None, caption="", tag=None, features=()):
    """A camera-scan found item: templated notes like item_intake.questions.private_notes."""
    lines = [caption, f"Category: {cat.title()} ({round(100 * conf)}% sure).",
             "Colours: " + ", ".join(f"{c} {round(100 * s)}%" for c, s in colours) + "."]
    if features:
        lines.append("Features: " + ", ".join(features) + ".")
    if contents:
        lines.append("Inside: " + ", ".join(contents) + ".")
    if brand:
        lines.append(f"Brand: {brand}.")
    if tag:
        lines.append(f'Text on tag: "{tag}".')
    lines.append("[Auto-logged by camera scan]")
    return {"notes": " ".join(x for x in lines if x), "category": cat, "captureMode": "camera_scan",
            "attributes": {"category": {"value": cat, "confidence": conf},
                           "colours": [{"name": c, "share": s} for c, s in colours]}}


def _found(fid, date, trip, bus, seat=None, **kw):
    doc = {"found_id": fid, "route_id": ROUTE, "date": date, "trip_id": trip, "bus_id": bus,
           "found_seat_area": seat, "ocr_text": kw.pop("ocr", ""), "category": kw.pop("category", None),
           "public_description": kw.pop("public", "Item"), "depot_phone": "9000000001"}
    doc.update(kw)
    return doc


def _lost(rid, date, trip, bus, desc, category, seat=None, name="Passenger", phone="+919000000000", truth=None):
    return {"request_id": rid, "tracking_id": rid.upper(), "route_id": ROUTE, "date": date, "trip_id": trip,
            "bus_id": bus, "seat_no": seat, "description": desc, "category": category, "name": name,
            "phone": phone, "status": "pending", "truth": truth}


D1, D2, D3, D4, D5 = "2026-11-01", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"

FOUND = [
    # ── trip T1 (bus B1, Nov 1): five black bags + others ──────────────────
    _found("f-wildcraft", D1, "T1", "B1", 14, public="Black backpack", ocr="WILDCRAFT",
           **_scan("bag", 0.93, [("black", 0.71), ("grey", 0.12)], ["laptop", "charger"], "Wildcraft",
                   "A black backpack with two zip pockets.")),
    _found("f-tiffinbag", D1, "T1", "B1", 22, public="Black bag",
           **_scan("bag", 0.88, [("black", 0.8)], ["tiffin", "bottle"], None, "A black cloth bag.")),
    _found("f-schoolbag", D1, "T1", "B1", 31, public="Black school bag",
           **_scan("bag", 0.9, [("black", 0.6), ("blue", 0.2)], ["book"], "Skybags",
                   "A black and blue school bag.", tag="ANU 7B")),
    _found("f-handbag", D1, "T1", "B1", 8, public="Black handbag", category="bag",
           notes="Black ladies handbag, keys and a small purse inside. Left on seat 8."),
    _found("f-blackbag-clothes", D1, "T1", "B1", 40, public="Black bag", category="bag",
           notes="Big black travel bag with clothes and a towel, found in the boot."),
    _found("f-redumbrella", D1, "T1", "B1", 3, public="Red umbrella", category="umbrella",
           notes="Red folding umbrella near the front door."),
    _found("f-samsung-tab", D1, "T1", "B1", 12, public="Tablet", ocr="SAMSUNG",
           **_scan("laptop", 0.84, [("silver", 0.5), ("black", 0.3)], (), "Samsung", "A silver tablet.")),
    # ── trip T2 (bus B2, Nov 1) ─────────────────────────────────────────────
    _found("f-name-tag", D1, "T2", "B2", 18, public="Red suitcase", ocr="ARUN KUMAR 98765 43210",
           **_scan("bag", 0.95, [("red", 0.75)], (), "American Tourister", "A red trolley suitcase.",
                   tag="ARUN KUMAR", features=("wheels", "name tag"))),
    _found("f-green-suitcase", D1, "T2", "B2", 20, public="Green suitcase", category="bag",
           notes="Green hard suitcase with wheels, locked."),
    _found("f-hp-laptop", D1, "T2", "B2", 9, public="Laptop", ocr="hp 5CG1234XYZ",
           **_scan("laptop", 0.92, [("grey", 0.6)], (), "HP", "A grey laptop.")),
    # ── trip T3 (bus B1 again, Nov 2: the bus continues) ───────────────────
    _found("f-tanglish", D2, "T3", "B1", 27, public="Black bag",
           **_scan("bag", 0.86, [("black", 0.66)], ["tiffin", "charger"], None, "A black cloth bag.")),
    _found("f-blue-medicine", D2, "T3", "B1", 5, public="Blue bag", category="bag",
           notes="Blue sling bag with medicine strips and spectacles."),
    _found("f-blue-books", D2, "T3", "B1", 6, public="Blue bag", category="bag",
           notes="Blue backpack with notebooks and a geometry box."),
    _found("f-lenskart", D2, "T3", "B1", 11, public="Spectacles", ocr="LENSKART",
           **_scan("spectacles", 0.9, [("blue", 0.55)], (), "Lenskart", "Spectacles in a blue case.")),
    _found("f-umbrella-a", D2, "T3", "B1", 15, public="Black umbrella", category="umbrella",
           notes="Black umbrella, long handle."),
    _found("f-umbrella-b", D2, "T3", "B1", 16, public="Black umbrella", category="umbrella",
           notes="Black umbrella, long handle, slightly torn."),
    # ── trip T4 (bus B3, Nov 2) ─────────────────────────────────────────────
    _found("f-purse", D2, "T4", "B3", 2, public="Brown purse", category="wallet",
           notes="Brown leather purse with an ATM card, a bus ticket and some cash."),
    _found("f-documents", D2, "T4", "B3", 19, public="Blue folder", ocr="PRIYA RAMAN MARKSHEET",
           category="documents", notes="Blue plastic folder with marksheets and certificates, name Priya Raman."),
    _found("f-hoodie", D2, "T4", "B3", 25, public="Grey hoodie", ocr="PUMA", category="clothing",
           notes="Grey Puma hoodie, size M."),
    _found("f-skybags", D2, "T4", "B3", 30, public="Blue backpack", ocr="WILDCRAFT",
           **_scan("bag", 0.9, [("blue", 0.7)], ["clothes"], "Wildcraft", "A blue backpack.")),
    # ── trip T5 (bus B4, Nov 3) ─────────────────────────────────────────────
    _found("f-phone-imei", D3, "T5", "B4", 7, public="Phone", ocr="IMEI 356789104455667",
           **_scan("phone", 0.94, [("black", 0.7)], (), "Redmi", "A black phone in a red cover.")),
    _found("f-pink-bottle", D3, "T5", "B4", 13, public="Pink bottle", category="other",
           notes="Pink steel water bottle, Milton."),
    _found("f-orphan-cap", D3, "T5", "B4", 33, public="Cap", category="clothing",
           notes="White cricket cap."),
    _found("f-navy-backpack", D3, "T5", "B4", 21, public="Dark backpack",
           **_scan("bag", 0.9, [("black", 0.55), ("blue", 0.3)], ["charger"], None, "A dark navy backpack.")),
    # ── Nov 4-5: late finds and the pre-journey log ─────────────────────────
    _found("f-late-trolley", D5, "T6", "B2", None, public="Purple suitcase", category="bag",
           notes="Purple trolley suitcase with a yellow ribbon on the handle."),
    _found("f-before-journey", D1, "T2", "B2", 4, public="Brown wallet", category="wallet",
           notes="Brown wallet with cards."),
]

LOST = [
    # five black bags on T1 — told apart only by brand / contents / marks
    _lost("l-wildcraft", D1, "T1", "B1", "Black Wildcraft backpack with my Dell laptop and charger", "bag", 14,
          truth="f-wildcraft"),
    _lost("l-tiffinbag", D1, "T1", "B1", "black bag, tiffin box and a water bottle inside", "bag", 21,
          truth="f-tiffinbag"),
    _lost("l-schoolbag", D1, "T1", "B1", "My daughter's black and blue Skybags school bag with books, name tag ANU",
          "bag", 30, name="Anu S", truth="f-schoolbag"),
    _lost("l-handbag", D1, "T1", "B1", "Black handbag with house keys and a small purse", "bag", 8,
          truth="f-handbag"),
    _lost("l-clothesbag", D1, "T1", "B1", "Large black travel bag with clothes and a towel, kept in the boot", "bag",
          truth="f-blackbag-clothes"),
    # same trip, different item: must NOT match the bags on T1
    _lost("l-red-umbrella", D1, "T1", "B1", "red umbrella", "umbrella", 4, truth="f-redumbrella"),
    _lost("l-samsung-phone", D1, "T1", "B1", "Samsung phone, black, in a blue cover", "phone", 12, truth=None),
    # name tag + phone number, T2
    _lost("l-name-tag", D1, "T2", "B2", "Red American Tourister trolley suitcase", "bag", 18,
          name="Arun Kumar", phone="+919876543210", truth="f-name-tag"),
    _lost("l-red-vs-green", D1, "T2", "B2", "red suitcase with wheels", "bag", 20, truth=None),
    _lost("l-hp-serial", D1, "T2", "B2", "HP laptop, grey, serial 5CG1234XYZ", "laptop", 9, truth="f-hp-laptop"),
    # Tanglish on T1 → found on the bus's next run T3
    _lost("l-tanglish", D1, "T1", "B1", "karuppu pai, ulle tiffin matrum charger", "bag", 27, truth="f-tanglish"),
    _lost("l-tamil", D2, "T3", "B1", "நீல பை, உள்ளே மருந்து மற்றும் கண்ணாடி", "bag", 5, truth="f-blue-medicine"),
    _lost("l-blue-books", D2, "T3", "B1", "blue backpack with my notebooks", "bag", 6, truth="f-blue-books"),
    _lost("l-lenskart", D2, "T3", "B1", "Lenskart spectacles in a blue case", "spectacles", 11, truth="f-lenskart"),
    _lost("l-umbrella", D2, "T3", "B1", "black umbrella", "umbrella", 15, truth="f-umbrella-a"),
    # T4
    _lost("l-purse", D2, "T4", "B3", "brown ladies purse, ATM card and bus ticket inside", "bag", 2,
          truth="f-purse"),
    _lost("l-documents", D2, "T4", "B3", "file with my certificates and marksheets", "documents", 19,
          name="Priya Raman", truth="f-documents"),
    _lost("l-hoodie", D2, "T4", "B3", "grey Puma hoodie", "clothing", 25, truth="f-hoodie"),
    _lost("l-skybags-conflict", D2, "T4", "B3", "blue Skybags backpack with clothes", "bag", 30, truth=None),
    # T5
    _lost("l-imei", D3, "T5", "B4", "Redmi phone black with red cover IMEI 356789104455667", "phone", 7,
          truth="f-phone-imei"),
    _lost("l-bottle", D3, "T5", "B4", "pink Milton water bottle", "other", 13, truth="f-pink-bottle"),
    _lost("l-navy", D3, "T5", "B4", "navy blue backpack with a charger", "bag", 21, truth="f-navy-backpack"),
    # duplicate report of the same item (same passenger reported twice)
    _lost("l-navy-dup", D3, "T5", "B4", "navy blue backpack, charger inside", "bag", 21, truth="f-navy-backpack"),
    # vague report
    _lost("l-vague", D3, "T5", "B4", "bag", "bag", truth=None),
    # different bus on the same day and route
    _lost("l-other-bus", D1, "T9", "B9", "Green hard suitcase with wheels", "bag", truth=None),
    # found item logged a day before this passenger's journey
    _lost("l-after-log", D3, "T7", "B2", "brown wallet with cards", "wallet", truth=None),
    # a late find (4 days later), different trip on the same bus
    _lost("l-late", D1, "T2", "B2", "purple trolley suitcase, yellow ribbon on the handle", "bag",
          truth="f-late-trolley"),
]


def window(date: str, days: int, forward: bool):
    from datetime import datetime, timedelta
    d0 = datetime.strptime(date, "%Y-%m-%d")
    rng = range(0, days + 1)
    return {(d0 + (timedelta(days=i) if forward else -timedelta(days=i))).strftime("%Y-%m-%d") for i in rng}


def found_candidates(lost):
    dates = window(lost["date"], 7, forward=True)
    return [f for f in FOUND if f["route_id"] == lost["route_id"] and f["date"] in dates]


def lost_candidates(found):
    dates = window(found["date"], 7, forward=False)
    return [lo for lo in LOST if lo["route_id"] == found["route_id"] and lo["date"] in dates]
