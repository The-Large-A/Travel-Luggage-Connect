"""Every town in the seed has a REAL coordinate (no random fallback), so maps and the
'depots after your stop' logic follow actual geography."""
import csv
import math
import os

import seed as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _csv_cities():
    rows = list(csv.DictReader(open(os.path.join(ROOT, st.CSV_PATH), encoding="utf-8-sig")))
    return {st.canon(r[k]) for r in rows for k in ("From", "To")} - {""}


def test_every_csv_town_has_a_real_coordinate():
    missing = sorted(c for c in _csv_cities() if c not in st.CITY_COORDS)
    assert not missing, missing


def test_depot_towns_have_real_coordinates():
    towns = {st.canon(c) for c in st.DEPOT_HOME.values()} if hasattr(st, "DEPOT_HOME") else set()
    assert all(t in st.CITY_COORDS for t in towns), sorted(t for t in towns if t not in st.CITY_COORDS)


def test_coordinates_are_in_south_india():
    for city, (lat, lon) in st.CITY_COORDS.items():
        assert 8.0 <= lat <= 13.8 and 75.5 <= lon <= 80.5, city


def test_known_towns_are_where_they_should_be():
    def km(a, b):
        return 111 * math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(math.radians(a[0])))
    truth = {"RAJAPALAYAM": (9.45, 77.55), "VANDAVASI": (12.50, 79.62), "VANIYAMBADI": (12.68, 78.62),
             "CHENNAI": (13.08, 80.27), "MADURAI": (9.93, 78.12), "TENKASI": (8.96, 77.30)}
    for city, where in truth.items():
        assert km(st.CITY_COORDS[city], where) < 15, city
