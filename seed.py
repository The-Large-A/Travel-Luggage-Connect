"""
seed.py — every piece of demo data, in one place.

    python start.py            # normal use: start.py seeds a fresh database for you
    python seed.py             # seed the database in MONGO_URI (needs SIGNING_SECRET)

What it writes (all idempotent, in this order):

  1. Depot logins       the 5 named depot accounts (password pass123), kept by step 2
  2. Transport network  from the official SETC CSV (data/SETCbustimings_1_0.csv),
                        scaled up with synthetic Tamil Nadu data: 100 depots,
                        3000 stops, 500 routes, 500 buses, trips from real
                        departure timings, drivers, conductors, bus schedules
  3. Tickets            the mock TNSTC PNR database (AES-256-GCM sealed rows) and the
                        mock UIDAI (Aadhaar) database, each ticket linked to a real
                        trip; the ticket's route text is the linked bus route

There are no sample lost reports, found items, matches or claims: the review
starts from a clean slate. The review login is REVIEW_LOGIN below.
"""

import csv
import hashlib
import os
import re
import random
from datetime import timedelta

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

import transport_schema as T
from passenger_auth import ensure_auth_indexes
from passenger_auth.identity import SEALED_SCHEMA, booking_id_proof, seed_mock_uidai
from passenger_auth.identity_service import ensure_identity_indexes
from passenger_auth.phone import normalize_phone
from passenger_auth.stores import BOOKINGS, BookingStore, drop_legacy_indexes
from passenger_auth.timeutil import to_db, today_ist, utcnow
from passenger_auth.vault import Vault

DB_NAME = "tn_bus_lost_found"
SEED_VERSION = 4                     # bump to make start.py rebuild every local database

# The passenger and depot accounts handed to the review panel.
REVIEW_LOGIN = {
    "passenger": {"name": "Divya Prakash", "aadhaar": "6123 4567 8904", "pnr": "T26100107", "phone": "+91 95000 11122"},
    # Further along the passenger's route than where they got off, so the item can be there.
    "depot": {"name": "Coimbatore Depot", "phone": "9000000002", "password": "pass123"},
}

# ═══ 1. Depot logins ═══════════════════════════════════════════════════════
# The transport seed (step 2) keeps these phones and passwords for the depot in
# the same city, so they stay the staff logins.
DEPOT_LOGINS = [
    {"phone": "9000000001", "name": "Chennai Depot", "password": "pass123", "stop": "Chennai",
     "routes": ["ch-co", "ch-md"]},
    {"phone": "9000000002", "name": "Coimbatore Depot", "password": "pass123", "stop": "Coimbatore",
     "routes": ["ch-co"]},
    {"phone": "9000000003", "name": "Madurai Depot", "password": "pass123", "stop": "Madurai",
     "routes": ["ch-md", "md-tn"]},
    {"phone": "9000000004", "name": "Salem Depot", "password": "pass123", "stop": "Salem", "routes": ["ch-co"]},
    {"phone": "9000000005", "name": "Tirunelveli Depot", "password": "pass123", "stop": "Tirunelveli",
     "routes": ["md-tn"]},
]


def seed_depot_logins(db) -> int:
    """Only on an empty depots collection (the transport seed then expands it)."""
    if db[T.DEPOTS].estimated_document_count():
        return 0
    db[T.DEPOTS].insert_many([dict(d) for d in DEPOT_LOGINS])
    return len(DEPOT_LOGINS)


# ═══ 2. Transport network ══════════════════════════════════════════════════
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "SETCbustimings_1_0.csv")


# Target sizes
N_DEPOTS = 100
N_STOPS  = 3000
N_ROUTES = 500
N_BUSES  = 500

# Tamil Nadu geographic bounding box (for deterministic fallback coordinates)
TN_LAT = (8.08, 13.50)
TN_LON = (76.23, 80.35)

# ── Curated real coordinates for the well-known cities in the CSV ─────────────
CITY_COORDS = {
    "CHENNAI": (13.0827, 80.2707), "COIMBATORE": (11.0168, 76.9558),
    "MADURAI": (9.9252, 78.1198), "TRICHY": (10.7905, 78.7047),
    "SALEM": (11.6643, 78.1460), "TIRUNELVELI": (8.7139, 77.7567),
    "TUTICORIN": (8.7642, 78.1348), "NAGERCOIL": (8.1780, 77.4285),
    "KANYAKUMARI": (8.0883, 77.5385), "VELLORE": (12.9165, 79.1325),
    "ERODE": (11.3410, 77.7172), "THANJAVUR": (10.7870, 79.1378),
    "DINDIGUL": (10.3624, 77.9695), "KARUR": (10.9601, 78.0766),
    "NAMAKKAL": (11.2189, 78.1677), "DHARMAPURI": (12.1211, 78.1583),
    "HOSUR": (12.7409, 77.8253), "BANGALORE": (12.9716, 77.5946),
    "TIRUPATHI": (13.6288, 79.4192), "PUDUKKOTTAI": (10.3833, 78.8001),
    "PUDUKOTTAI": (10.3833, 78.8001), "KARAIKUDI": (10.0736, 78.7734),
    "SIVAGANGAI": (9.8433, 78.4809), "RAMNAD": (9.3639, 78.8395),
    "VIRUDHUNAGAR": (9.5851, 77.9578), "SIVAKASI": (9.4533, 77.7970),
    "KOVILPATTI": (9.1710, 77.8652), "SANKARANKOIL": (9.1697, 77.5459),
    "RAMESWARAM": (9.2876, 79.3129), "POLLACHI": (10.6588, 77.0088),
    "PALANI": (10.4500, 77.5161), "KODAIKANAL": (10.2381, 77.4892),
    "OOTY": (11.4102, 76.6950), "UDUMALPET": (10.5883, 77.2450),
    "TIRUPPUR": (11.1085, 77.3411), "METTUPALAYAM": (11.2996, 76.9370),
    "VILLUPURAM": (11.9401, 79.4861), "PUDUCHERRY": (11.9416, 79.8083),
    "CHIDAMBARAM": (11.3994, 79.6913), "MAYILADUTHURAI": (11.1018, 79.6552),
    "MYLADUTHURAI": (11.1018, 79.6552), "NAGAPATTINAM": (10.7660, 79.8420),
    "VELANKANNI": (10.6810, 79.8500), "VEDARANYAM": (10.3746, 79.8500),
    "TIRUVARUR": (10.7726, 79.6368), "MANNARKUDI": (10.6667, 79.4500),
    "MANNACHANALLUR": (10.9200, 78.6500), "KUMBAKONAM": (10.9601, 79.3788),
    "PATTUKOTTAI": (10.4250, 79.3190), "ARANTHANGI": (10.1720, 79.0030),
    "DEVAKOTTAI": (9.9470, 78.8230), "PARAMAKUDI": (9.5480, 78.5900),
    "THENI": (10.0104, 77.4768), "CUMBAM": (9.7333, 77.2833),
    "BODI": (10.0100, 77.3500), "PERIYAKULAM": (10.1200, 77.5500),
    "USILAMPATTI": (9.9670, 77.7830), "TIRUMANGALAM": (9.8200, 77.9900),
    "SRIVILLIPUTHUR": (9.5120, 77.6330), "TIRUCHENDUR": (8.4959, 78.1197),
    "MARTHANDAM": (8.3010, 77.2230), "KOLLAM": (8.8932, 76.6141),
    "TRIVANDRUM": (8.5241, 76.9366), "ERNAKULAM": (9.9816, 76.2999),
    "TRISSUR": (10.5276, 76.2144), "GURUVAYUR": (10.5946, 76.0400),
    "KOZHICODE": (11.2588, 75.7804), "KOLLENCODE": (8.2500, 77.2200),
    "KOTTARAKARA": (9.0000, 76.7800), "PATHANAMTHITTA": (9.2648, 76.7870),
    "CHENGANACHERRY": (9.3160, 76.5220), "KUMILI": (9.6000, 77.1600),
    "GOBI": (11.4550, 77.4420), "DHARAPURAM": (10.7350, 77.5320),
    "MELUR": (10.0330, 78.3390), "NATHAM": (10.2270, 78.2260),
    "KALPAKKAM": (12.5200, 80.1750), "TAMBARAM": (12.9249, 80.1000),
    "TIRUVANMIYUR": (12.9830, 80.2590), "AVUDAIYAR KOIL": (10.0330, 78.7500),
    "AVUDAIARKOIL": (10.0330, 78.7500), "ANNAVASAL": (10.4000, 78.7000),
    "ALANKULAM": (8.8670, 77.4830), "ARANI": (12.6700, 79.2900),
    "ARIMALAM": (10.2500, 78.8830), "ARUPPUKOTTAI": (9.5090, 78.0970),
    "KAMUDHI": (9.4000, 78.3700), "SAYALKUDI": (9.1700, 78.4500),
    "KAZHUGUMALAI": (9.1500, 77.6000), "IDAYANKUDI": (8.4000, 77.9000),
    "UDANKUDI": (8.4900, 78.0400), "KULASEKARAPATTINAM": (8.3930, 78.0850),
    "KULASEKARAPATTI": (8.3930, 78.0850), "KULASEKARAM": (8.3000, 77.2500),
    "KARUNGAL": (8.2500, 77.2900), "SHENCOTTAH": (8.9760, 77.2510),
    "GANDARVAKOTTAI": (10.4500, 78.9000), "MANALMELKUDI": (10.0400, 79.2500),
    "KEERAMANGALAM": (10.2000, 78.9000), "PONNAMARAVATHY": (10.2800, 78.6300),
    "TIRUMAYAM": (10.2500, 78.7500), "ILUPPUR": (10.5100, 78.6300),
    "KULATHUR": (10.4000, 78.6000), "PULLAMBADI": (10.9000, 78.8500),
    "SRIRANGAM": (10.8620, 78.6900), "POOMBUHAR": (11.1400, 79.8500),
    "TIRUNALLAR": (10.9260, 79.7900), "TIRUTHURAIPOOND": (10.5300, 79.6400),
    "MEEMISAL": (10.0500, 79.2000), "TONDI": (9.7420, 79.0190),
    "PERIYAPATTINAM": (9.2700, 78.9200), "KEEZHAKARAI": (9.2340, 78.7860),
    "PERAIYUR": (9.7000, 77.7800), "VEDASANDUR": (10.3670, 77.9500),
    "PALLAPATTI": (10.4000, 78.0000), "SATTAN KULAM": (8.4500, 77.9200),
    "THYSAYANVILAI": (8.3300, 77.9000), "ERUVADI": (8.4700, 77.5300),
    "SIVAGANGAI": (9.8433, 78.4809), "GUDALORE": (11.5000, 76.5000),
    "KUMBAKONAM": (10.9601, 79.3788), "TIRUTHANGAL": (9.4930, 77.8390),
    # Towns that previously fell back to a random point in the TN box (which made routes
    # zigzag on the map and broke 'depots after your stop'). Looked up once on OpenStreetMap
    # Nominatim (Nov 2026) and checked by hand; towns that share a district's name were
    # moved from the district centre to the town itself.
    "AMBASAMUDRAM": (8.7100, 77.4500), "AMBUR": (12.7874, 78.7188),
    "ANDIPATTI": (9.8034, 77.5269), "ANTHIYUR": (11.7144, 77.5679),
    "ARAKKONAM": (13.0301, 79.6088), "ARIYALUR": (11.1401, 79.0786),
    "ARNI": (12.6677, 79.2853), "ATTUR": (10.2878, 77.8521),
    "AVINASHI": (11.1900, 77.2680), "BATLAGUNDU": (10.1636, 77.7591),
    "BHAVANI": (11.4700, 77.3312), "BOOTHAPANDI": (8.2700, 77.4300),
    "CHENGALPATTU": (12.6841, 79.9836), "CHERANMAHADEVI": (8.6793, 77.5617),
    "CHETPET": (12.4662, 79.3507), "CHEYYAR": (12.6564, 79.5405),
    "CHINNAMANUR": (9.8416, 77.3849), "COLACHEL": (8.1753, 77.2519),
    "CUDDALORE": (11.7480, 79.7714), "ETTAYAPURAM": (9.1806, 78.0231),
    "GINGEE": (12.2544, 79.4158), "KADAYANALLUR": (9.0842, 77.3462),
    "KALLAKURICHI": (11.7383, 78.9591), "KANCHIPURAM": (12.8342, 79.7036),
    "KANGEYAM": (10.9903, 77.6282), "KARIAPATTI": (9.6695, 78.1646),
    "KAYALPATTINAM": (8.5678, 78.1004), "KRISHNAGIRI": (12.5186, 78.2137),
    "KUTTAM": (8.3203, 77.9389), "LALGUDI": (10.8754, 78.8153),
    "MADURANTAKAM": (12.5082, 79.8882), "MANAPPARAI": (10.6082, 78.4231),
    "MARAIMALAI NAGAR": (12.7958, 80.0269), "MARAKKANAM": (12.1959, 79.9442),
    "MELAPALAYAM": (8.7009, 77.7160), "METTUR": (11.7928, 77.8649),
    "MUSIRI": (11.0488, 78.5282), "NAKUDI": (10.1500, 79.1070),
    "NANGUNERI": (8.4893, 77.6597), "NEYVELI": (11.5360, 79.4824),
    "NILAKOTTAI": (10.2437, 77.8727), "ODDANCHATRAM": (10.4851, 77.7460),
    "OMALUR": (11.7429, 78.0473), "OPPILAN": (9.1424, 78.5061),
    "OTTANCHATRAM": (10.4851, 77.7460), "PADMANABHAPURAM": (8.2523, 77.3265),
    "PALAYAMKOTTAI": (8.7197, 77.7644), "PALLADAM": (10.9708, 77.2817),
    "PANRUTI": (11.7723, 79.5519), "PAPANASAM": (10.8604, 79.2358),
    "PERAMBALUR": (11.2342, 78.8807), "PERUNDURAI": (11.2539, 77.6288),
    "POLUR": (12.5938, 79.1385), "PULIYANGUDI": (9.1756, 77.3966),
    "RAJAPALAYAM": (9.4032, 77.5183), "RASIPURAM": (11.4537, 78.1785),
    "SANKAGIRI": (11.4761, 77.8704), "SATHYAMANGALAM": (11.5524, 77.2654),
    "SATTUR": (9.3227, 77.9418), "SENGOTTAI": (8.9873, 77.2422),
    "SHOLAVANDAN": (10.0227, 77.9654), "SRIPERUMBUDUR": (12.9666, 79.9458),
    "SRIVAIKUNTAM": (8.6962, 77.9022), "SULUR": (10.9891, 77.1015),
    "TENKASI": (8.9594, 77.3152), "THENI ALLINAGARAM": (10.0104, 77.4768),
    "THUCKALAY": (8.2584, 77.3112), "THURAIYUR": (11.2302, 78.5651),
    "TINDIVANAM": (12.2262, 79.6515), "TIRUCHENGODE": (11.3787, 77.8949),
    "TIRUPATHURRMD": (10.1200, 78.6000), "TIRUVANNAMALAI": (12.2253, 79.0747),
    "TNV": (8.7139, 77.7567), "UTHAMAPALAYAM": (9.7663, 77.3299),
    "UTHIRAMERUR": (12.6494, 79.7806), "VADIPATTI": (10.0790, 78.0360),
    "VALLIYOOR": (8.3833, 77.6167), "VALPARAI": (10.3280, 76.9557),
    "VANDAVASI": (12.5056, 79.6050), "VANIYAMBADI": (12.6787, 78.6200),
    "VILATHIKULAM": (9.1682, 78.1997), "VILAVANCODE": (8.3831, 77.2056),
    "WATRAP": (9.6365, 77.6397),
}

# Depot code → home city. Inferred from CSV (non-hub argmax) with overrides for
# the Chennai-family / ambiguous codes.
DEPOT_HOME_OVERRIDE = {
    "CB": "CHENNAI", "CC": "CHENNAI", "CA": "CHENNAI",
    "PDY": "PUDUCHERRY", "DGL": "DINDIGUL", "NGP": "NAGAPATTINAM",
}
HUB_CITIES = {"CHENNAI", "BANGALORE", "TIRUPATHI", "TAMBARAM", "PUDUCHERRY"}

# Real district for major depot cities (others default to the city name).
CITY_DISTRICT = {
    "CHENNAI": "Chennai", "COIMBATORE": "Coimbatore", "MADURAI": "Madurai",
    "TRICHY": "Tiruchirappalli", "SALEM": "Salem", "TIRUNELVELI": "Tirunelveli",
    "TUTICORIN": "Thoothukudi", "NAGERCOIL": "Kanniyakumari", "VELLORE": "Vellore",
    "ERODE": "Erode", "THANJAVUR": "Thanjavur", "DINDIGUL": "Dindigul",
    "KARUR": "Karur", "NAMAKKAL": "Namakkal", "DHARMAPURI": "Dharmapuri",
    "HOSUR": "Krishnagiri", "PUDUKKOTTAI": "Pudukkottai", "KARAIKUDI": "Sivagangai",
    "PUDUCHERRY": "Puducherry", "VILLUPURAM": "Viluppuram", "MARTHANDAM": "Kanniyakumari",
    "SHENCOTTAH": "Tenkasi", "NAGAPATTINAM": "Nagapattinam", "KUMBAKONAM": "Thanjavur",
}

# Synthetic TN town pool for extra depots/stops (not necessarily in the CSV).
TN_TOWN_POOL = [
    "Arakkonam", "Ambur", "Vaniyambadi", "Krishnagiri", "Tiruvannamalai",
    "Cuddalore", "Neyveli", "Panruti", "Kallakurichi", "Ariyalur",
    "Perambalur", "Musiri", "Thuraiyur", "Manapparai", "Lalgudi",
    "Rasipuram", "Tiruchengode", "Sankagiri", "Attur", "Mettur",
    "Omalur", "Bhavani", "Sathyamangalam", "Perundurai", "Kangeyam",
    "Palladam", "Valparai", "Sulur", "Avinashi", "Anthiyur",
    "Theni Allinagaram", "Andipatti", "Chinnamanur", "Uthamapalayam",
    "Melur", "Vadipatti", "Sholavandan", "Nilakottai", "Oddanchatram",
    "Batlagundu", "Ottanchatram", "Kariapatti", "Rajapalayam", "Sattur",
    "Watrap", "Srivaikuntam", "Kayalpattinam", "Ettayapuram", "Vilathikulam",
    "Nanguneri", "Ambasamudram", "Tenkasi", "Kadayanallur", "Puliyangudi",
    "Sengottai", "Valliyoor", "Cheranmahadevi", "Palayamkottai", "Melapalayam",
    "Colachel", "Thuckalay", "Boothapandi", "Vilavancode", "Padmanabhapuram",
    "Gingee", "Tindivanam", "Marakkanam", "Vandavasi", "Chetpet",
    "Polur", "Arni", "Cheyyar", "Kanchipuram", "Sriperumbudur",
    "Maraimalai Nagar", "Chengalpattu", "Madurantakam", "Uthiramerur", "Walajabad",
    "Ranipet", "Arcot", "Gudiyatham", "Pernambut", "Tirupathur",
    "Harur", "Palacode", "Pennagaram", "Karimangalam", "Nagercoil South",
    "Manamadurai", "Ilayangudi", "Tirupattur RMD", "Kalaiyarkoil", "Singampunari",
]

LOCALITY_ROOTS = [
    "Anna", "Gandhi", "Nehru", "Bharathi", "Kamaraj", "Periyar", "Sivan",
    "Murugan", "Amman", "Vinayagar", "Meenakshi", "Kaveri", "Vaigai",
    "Thamirabarani", "Kurinji", "Mullai", "Marutham", "Neithal", "Palar",
    "Ponni", "Senthil", "Velan", "Arasu", "Thendral", "Malligai",
]
LOCALITY_SUFFIX = ["Nagar", "Pudur", "Kulam", "Puram", "Patti", "Kudi",
                   "Palayam", "Colony", "Bus Stand", "Junction", "Thoppu", "Medu"]

TAMIL_FIRST = [
    "Arun", "Bala", "Chandran", "Dinesh", "Ezhil", "Ganesan", "Hari",
    "Iniyan", "Jagan", "Karthik", "Lokesh", "Manikandan", "Naveen", "Prabu",
    "Raja", "Saravanan", "Tamil", "Vijay", "Anand", "Murugan", "Selvam",
    "Kumar", "Senthil", "Ravi", "Suresh", "Ramesh", "Mohan", "Gopal",
    "Velan", "Arivu", "Kannan", "Pandian", "Sundar", "Vetri", "Mani",
]
TAMIL_LAST = [
    "Kumar", "Raj", "Murthy", "Krishnan", "Nathan", "Perumal", "Samy",
    "Pillai", "Nadar", "Gounder", "Thevar", "Chettiar", "Mudaliar", "Iyer",
    "Subramanian", "Rajan", "Moorthy", "Durai", "Palani", "Sekar",
]


def canon(city: str) -> str:
    """Normalize a raw CSV city to a canonical uppercase name for lookups."""
    c = re.sub(r"\(.*?\)", "", city)          # drop "(Via ...)", "(OOTY)" etc.
    c = re.split(r"\s+via\s+", c, flags=re.I)[0]
    c = re.split(r"[-/]", c)[0]               # "TRICHY-BHEL" -> "TRICHY"
    c = re.sub(r"\s+", " ", c).strip().upper()
    return c


def coord_for(city: str):
    """Real coordinate if known, else a deterministic point inside the TN box."""
    c = canon(city)
    if c in CITY_COORDS:
        return CITY_COORDS[c]
    h = int(hashlib.md5(c.encode()).hexdigest(), 16)
    lat = TN_LAT[0] + (h % 1000) / 1000 * (TN_LAT[1] - TN_LAT[0])
    lon = TN_LON[0] + ((h // 1000) % 1000) / 1000 * (TN_LON[1] - TN_LON[0])
    return (round(lat, 4), round(lon, 4))


def jitter(lat, lon, km=6.0):
    """Small deterministic-ish offset (~km) for a nearby minor stop."""
    d = km / 111.0
    return (round(lat + random.uniform(-d, d), 4), round(lon + random.uniform(-d, d), 4))


def route_type_from_suffix(route_no: str) -> str:
    m = re.search(r"([A-Z]+)$", route_no.strip().upper())
    suf = m.group(1) if m else "UD"
    return {
        "UD": "ULTRA DELUXE", "AC": "A/C", "TU": "ULTRA", "EU": "EXPRESS ULTRA",
        "AU": "A/C ULTRA", "BU": "ULTRA", "CU": "ULTRA", "KU": "ULTRA",
        "GU": "ULTRA", "CL": "DELUXE", "UK": "ULTRA", "VU": "ULTRA",
    }.get(suf, "ULTRA")


def reg_no(depot_code: str, idx: int) -> str:
    """TN-style registration, e.g. TN-72-N-1234."""
    # md5, NOT hash(): Python randomises string hashing per process, which would
    # change every bus's registration on each run and defeat the stable-id reseed.
    rto = 30 + (int(hashlib.md5(depot_code.encode()).hexdigest(), 16) % 60)   # 30..89
    letter = "ABCDEFGHJKLMNPRSTUVWXYZ"[idx % 22]
    return f"TN-{rto:02d}-{letter}-{1000 + (idx % 9000)}"


def parse_times(raw: str):
    """Split a CSV departure-timing cell into a clean list of HH.MM strings."""
    parts = re.split(r"[,\n]", raw.replace("\r", ""))
    out = []
    for p in parts:
        p = p.strip()
        if re.match(r"^\d{1,2}[.:]\d{0,2}$", p):
            hh, _, mm = p.replace(":", ".").partition(".")
            out.append(f"{int(hh):02d}.{(mm or '0').ljust(2, '0')[:2]}")
    return out


def est_arrival(dep: str, minutes: int) -> str:
    try:
        hh, mm = dep.split(".")
        base = int(hh) * 60 + int(mm)
        end = (base + minutes) % (24 * 60)
        return f"{end // 60:02d}.{end % 60:02d}"
    except Exception:
        return dep


def uid() -> str:
    return hashlib.md5(f"{random.random()}{random.random()}".encode()).hexdigest()


def load_id_map(db, coll: str, id_field: str, key_fields: list) -> dict:
    """
    Map an existing collection's NATURAL key -> its stored surrogate id.
    Used so a reseed reuses ids instead of minting new ones (see `stable`).
    """
    proj = {id_field: 1}
    for k in key_fields:
        proj[k] = 1
    out = {}
    for d in db[coll].find({}, proj):
        if d.get(id_field) is not None:
            out[tuple(d.get(k) for k in key_fields)] = d[id_field]
    return out


def stable(id_map: dict, *key) -> str:
    """
    Return the id already stored for this natural key, else a fresh one.

    This is what makes a reseed non-destructive: live lost/found reports hold
    route_id / trip_id / bus_id references, so regenerating those ids would
    orphan them. Reusing the existing id keeps every reference valid.
    """
    return id_map.get(tuple(key)) or uid()


# ══════════════════════════════════════════════════════════════════════════
def seed_transport(db):
    """The transport network (depots, stops, routes, buses, trips, crews, schedules)."""
    random.seed(42)                     # deterministic, reproducible seed data

    print("Applying schema validators...")
    T.apply_validators(db)

    # Existing natural-key -> id maps, so this run reuses ids (idempotent reseed).
    id_depot = load_id_map(db, T.DEPOTS, "depot_id", ["depot_code"])
    id_stop  = load_id_map(db, T.STOPS,  "stop_id",  ["name"])
    id_bus   = load_id_map(db, T.BUSES,  "bus_id",   ["registration_no"])
    id_route = load_id_map(db, T.ROUTES, "route_id", ["route_no", "origin_name", "dest_name"])
    id_trip  = load_id_map(db, T.TRIPS,  "trip_id",  ["route_id", "departure_time"])
    id_drv   = load_id_map(db, T.DRIVERS, "driver_id", ["employee_no"])
    id_con   = load_id_map(db, T.CONDUCTORS, "conductor_id", ["employee_no"])
    id_sched = load_id_map(db, T.BUS_SCHEDULES, "schedule_id", ["trip_id"])

    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    print(f"Loaded {len(rows)} SETC service rows from CSV")

    # ── Depot home cities (inference + overrides) ────────────────────────────
    from collections import Counter, defaultdict
    depot_city_counts = defaultdict(Counter)
    for r in rows:
        d = r["Depot"].strip()
        for city in (canon(r["From"]), canon(r["To"])):
            depot_city_counts[d][city] += 1

    real_codes = sorted(depot_city_counts.keys())
    depot_home = {}
    for code in real_codes:
        if code in DEPOT_HOME_OVERRIDE:
            depot_home[code] = DEPOT_HOME_OVERRIDE[code]
        else:
            non_hub = [c for c, _ in depot_city_counts[code].most_common()
                       if c not in HUB_CITIES]
            depot_home[code] = non_hub[0] if non_hub else \
                depot_city_counts[code].most_common(1)[0][0]

    # ── Preserve existing depot login accounts ───────────────────────────────
    existing = {d["phone"]: d for d in db[T.DEPOTS].find()}
    # Map an existing depot (by its 'stop'/'name' city) to a real code so we can
    # reuse its phone/password and keep current logins valid.
    def existing_for_city(city_upper):
        for phone, d in existing.items():
            if canon(d.get("stop", "")) == city_upper or canon(d.get("name", "")) == city_upper:
                return d
        return None

    # ── Build 100 depots: 22 real codes + synthetic fill ─────────────────────
    depots = []
    depot_by_code = {}
    phone_seq = 9000001000
    used_phones = set(existing.keys())

    def next_phone():
        nonlocal phone_seq
        phone_seq += 1
        while str(phone_seq) in used_phones:
            phone_seq += 1
        used_phones.add(str(phone_seq))
        return str(phone_seq)

    named_cities, claimed_logins = set(), set()
    for code in real_codes:
        home = depot_home[code]
        lat, lon = coord_for(home)
        prior = existing_for_city(home)
        # A city with several depot codes (Chennai CA/CB/CC) keeps the existing login
        # for one of them; the others get their own phone and a name with the code,
        # so no two depots ever share a staff login.
        if prior and prior["phone"] in claimed_logins:
            prior = None
        if prior:
            claimed_logins.add(prior["phone"])
        phone = prior["phone"] if prior else next_phone()
        password = prior.get("password", "pass123") if prior else "pass123"
        name = prior.get("name") if prior else (
            f"{home.title()} Depot ({code})" if home in named_cities else f"{home.title()} Depot")
        named_cities.add(home)
        doc = {
            "depot_id": stable(id_depot, code), "depot_code": code, "name": name,
            "phone": phone, "password": password,
            "city": home.title(), "district": CITY_DISTRICT.get(home, home.title()),
            "lat": lat, "lon": lon, "stop": home.title(),
            "routes": prior.get("routes", []) if prior else [],
            "is_staffed": True, "source": "setc_csv", "external_ref": None,
        }
        depots.append(doc)
        depot_by_code[code] = doc

    # Synthetic depots to reach 100
    town_i = 0
    while len(depots) < N_DEPOTS:
        town = TN_TOWN_POOL[town_i % len(TN_TOWN_POOL)]
        town_i += 1
        lat, lon = coord_for(town)
        code = f"SY{len(depots):03d}"
        doc = {
            "depot_id": stable(id_depot, code), "depot_code": code, "name": f"{town} Depot",
            "phone": next_phone(), "password": "pass123",
            "city": town, "district": town, "lat": lat, "lon": lon, "stop": town,
            "routes": [], "is_staffed": False, "source": "synthetic",
            "external_ref": None,
        }
        depots.append(doc)
        depot_by_code[code] = doc

    # Public contact numbers (fictitious) for the passenger contact drawer. The
    # login `phone` above is a staff credential and is never shown to passengers.
    # Derived by hashing (not `random`) so the seeded network stays byte-identical.
    for d in depots:
        h = int(hashlib.md5(d["depot_code"].encode()).hexdigest(), 16)
        d["publicPhone"] = f"+91 44 {2400 + h % 600:04d} {h // 600 % 9000 + 1000:04d}"
        d["whatsapp"] = f"+91 94450 {h // 7 % 90000 + 10000:05d}" if d["is_staffed"] else None

    # ── Stops: 137 real-city majors + synthetic minors to reach 3000 ─────────
    major_cities = sorted({canon(r[k]) for r in rows for k in ("From", "To")})
    # include depot home cities & synthetic towns as majors too
    for d in depots:
        major_cities.append(canon(d["city"]))
    major_cities = sorted(set(major_cities))

    stops = []
    stop_by_city = {}   # canonical major city -> stop doc
    for city in major_cities:
        lat, lon = coord_for(city)
        doc = {
            "stop_id": stable(id_stop, city.title()), "name": city.title(), "city": city.title(),
            "district": CITY_DISTRICT.get(city, city.title()),
            "lat": lat, "lon": lon, "depot_id": None, "is_major": True,
            "source": "setc_csv" if city in {canon(r[k]) for r in rows for k in ("From", "To")} else "synthetic",
            "external_ref": None,
        }
        stops.append(doc)
        stop_by_city[city] = doc

    # nearest depot for each major stop (simple squared-distance)
    def nearest_depot(lat, lon):
        best, bestd = None, 1e18
        for d in depots:
            dd = (d["lat"] - lat) ** 2 + (d["lon"] - lon) ** 2
            if dd < bestd:
                best, bestd = d, dd
        return best["depot_id"]

    for s in stops:
        s["depot_id"] = nearest_depot(s["lat"], s["lon"])

    # synthetic minor stops around major-city anchors until 3000
    anchor_cycle = [c for c in major_cities]
    ai = 0
    minor_idx = 0
    while len(stops) < N_STOPS:
        anchor = anchor_cycle[ai % len(anchor_cycle)]
        ai += 1
        a = stop_by_city[anchor]
        root = LOCALITY_ROOTS[minor_idx % len(LOCALITY_ROOTS)]
        suf = LOCALITY_SUFFIX[(minor_idx // len(LOCALITY_ROOTS)) % len(LOCALITY_SUFFIX)]
        minor_idx += 1
        lat, lon = jitter(a["lat"], a["lon"])
        minor_name = f"{root} {suf} ({anchor.title()})"
        stops.append({
            "stop_id": stable(id_stop, minor_name),
            "name": minor_name,
            "city": anchor.title(), "district": a["district"],
            "lat": lat, "lon": lon, "depot_id": a["depot_id"],
            "is_major": False, "source": "synthetic", "external_ref": None,
        })

    # pool of minor stops grouped by anchor city (for route intermediates)
    minors_by_city = defaultdict(list)
    for s in stops:
        if not s["is_major"]:
            minors_by_city[canon(s["city"])].append(s)

    # ── Routes: dedupe CSV services, then synthetic fill to 500 ──────────────
    def intermediates(o_stop, d_stop, k=4):
        """
        Pick up to k major stops that lie ALONG the origin→dest corridor.

        Each candidate is projected onto the origin→dest line: `t` is how far
        along the line it falls (0 at origin, 1 at dest) and `perp` is its
        perpendicular distance from the line. We keep only cities that are
        genuinely between the endpoints (0.05 < t < 0.95) and near the corridor
        (perp within ~0.6°, ≈65 km), then spread the picks evenly along `t` so
        the sequence is ordered and doesn't clump near one end. Projection —
        rather than raw distance-to-origin — is what prevents long routes from
        collecting a cluster of stops beside the origin and then jumping to the
        destination (which made the drawn road path appear to stop halfway).
        """
        olat, olon = o_stop["lat"], o_stop["lon"]
        dlat, dlon = d_stop["lat"], d_stop["lon"]
        vlat, vlon = dlat - olat, dlon - olon
        vlen2 = vlat * vlat + vlon * vlon
        if vlen2 == 0:
            return []
        cands = []
        for city, s in stop_by_city.items():
            if s["stop_id"] in (o_stop["stop_id"], d_stop["stop_id"]):
                continue
            t = ((s["lat"] - olat) * vlat + (s["lon"] - olon) * vlon) / vlen2
            if t <= 0.05 or t >= 0.95:
                continue   # not between origin and destination
            proj_lat, proj_lon = olat + t * vlat, olon + t * vlon
            perp = ((s["lat"] - proj_lat) ** 2 + (s["lon"] - proj_lon) ** 2) ** 0.5
            if perp > 0.6:
                continue   # too far off the corridor
            cands.append((t, s))
        cands.sort(key=lambda x: x[0])   # order ALONG the route
        if len(cands) <= k:
            return [s for _, s in cands]
        # spread picks evenly along the corridor instead of taking the first k
        step = len(cands) / k
        return [cands[int(i * step)][1] for i in range(k)]

    def build_sequence(o_city, d_city):
        o = stop_by_city[o_city]
        d = stop_by_city[d_city]
        seq = [o] + intermediates(o, d) + [d]
        # sprinkle one minor stop near a random intermediate for texture
        if len(seq) > 2 and minors_by_city.get(canon(seq[1]["city"])):
            seq.insert(2, random.choice(minors_by_city[canon(seq[1]["city"])]))
        # dedupe preserving order
        seen, ordered = set(), []
        for s in seq:
            if s["stop_id"] not in seen:
                seen.add(s["stop_id"]); ordered.append(s)
        return [s["stop_id"] for s in ordered]

    routes = []
    seen_routes = set()
    route_services = {}   # route_id -> No.of Service (frequency)
    for r in rows:
        rn = r["Route No."].strip().upper()
        o_city, d_city = canon(r["From"]), canon(r["To"])
        key = (rn, o_city, d_city)
        if key in seen_routes or o_city == d_city:
            continue
        if o_city not in stop_by_city or d_city not in stop_by_city:
            continue
        seen_routes.add(key)
        try:
            dist = int(r["Route Length"])
        except Exception:
            dist = 300
        try:
            svc = max(1, int(r["No.of Service"]))
        except Exception:
            svc = 1
        code = r["Depot"].strip()
        doc = {
            "route_id": stable(id_route, rn, o_city.title(), d_city.title()), "route_no": rn,
            "origin_stop_id": stop_by_city[o_city]["stop_id"],
            "dest_stop_id": stop_by_city[d_city]["stop_id"],
            "origin_name": o_city.title(), "dest_name": d_city.title(),
            "via": (r["To"] if "Via" in r["To"] or "via" in r["To"] else "").strip(),
            "distance_km": dist, "type": route_type_from_suffix(rn),
            "operating_depot_id": depot_by_code.get(code, depots[0])["depot_id"],
            "operating_depot_code": code,
            "stop_sequence": build_sequence(o_city, d_city),
            "departure_raw": r["Departure Timings"], "num_services": svc,
            "source": "setc_csv", "external_ref": None,
        }
        routes.append(doc)
        route_services[doc["route_id"]] = svc
        if len(routes) >= N_ROUTES:
            break

    # synthetic routes to reach 500 (random major city pairs)
    majors_list = [c for c in stop_by_city]
    syn_i = 0
    while len(routes) < N_ROUTES:
        o_city = majors_list[syn_i % len(majors_list)]
        d_city = majors_list[(syn_i * 7 + 3) % len(majors_list)]
        syn_i += 1
        if o_city == d_city:
            continue
        o, d = stop_by_city[o_city], stop_by_city[d_city]
        dist = int(((o["lat"] - d["lat"]) ** 2 + (o["lon"] - d["lon"]) ** 2) ** 0.5 * 111) + 40
        rn = f"{700 + len(routes)}SY"
        code = random.choice(real_codes)
        doc = {
            "route_id": stable(id_route, rn, o_city.title(), d_city.title()), "route_no": rn,
            "origin_stop_id": o["stop_id"], "dest_stop_id": d["stop_id"],
            "origin_name": o_city.title(), "dest_name": d_city.title(), "via": "",
            "distance_km": dist, "type": "ULTRA",
            "operating_depot_id": depot_by_code[code]["depot_id"],
            "operating_depot_code": code,
            "stop_sequence": build_sequence(o_city, d_city),
            "departure_raw": "", "num_services": 1,
            "source": "synthetic", "external_ref": None,
        }
        routes.append(doc)
        route_services[doc["route_id"]] = 1

    # Denormalized lowercase stop cities per route. Lets depot_serves_route()
    # and the depot_only route filter answer "does this route call at the depot's
    # city?" with one indexable query instead of resolving every stop document.
    city_by_stop_id = {s["stop_id"]: (s.get("city") or "").strip().lower()
                       for s in stops}
    for rt in routes:
        rt["stop_cities"] = sorted(
            {city_by_stop_id.get(sid, "") for sid in rt["stop_sequence"]} - {""}
        )

    # ── Buses: distribute 500 across depots weighted by routes operated ──────
    routes_by_depot = defaultdict(list)
    for rt in routes:
        routes_by_depot[rt["operating_depot_id"]].append(rt)

    operating_depots = [d for d in depots if routes_by_depot[d["depot_id"]]]
    # base 1 bus each, then distribute remainder by route-count weight
    bus_alloc = {d["depot_id"]: 1 for d in operating_depots}
    remaining = N_BUSES - len(operating_depots)
    total_routes = sum(len(routes_by_depot[d["depot_id"]]) for d in operating_depots)
    for d in operating_depots:
        share = int(remaining * len(routes_by_depot[d["depot_id"]]) / max(1, total_routes))
        bus_alloc[d["depot_id"]] += share
    # fix rounding drift to hit exactly 500
    drift = N_BUSES - sum(bus_alloc.values())
    for d in operating_depots[:abs(drift)]:
        bus_alloc[d["depot_id"]] += 1 if drift > 0 else -1

    buses = []
    buses_by_depot = defaultdict(list)
    for d in operating_depots:
        for i in range(bus_alloc[d["depot_id"]]):
            reg = reg_no(d["depot_code"], len(buses))
            doc = {
                "bus_id": stable(id_bus, reg), "registration_no": reg,
                "depot_id": d["depot_id"], "depot_code": d["depot_code"],
                "type": "ULTRA", "capacity": 0, "seat_layout": {},
                "model": random.choice(["Ashok Leyland Viking", "TATA LPO 1618",
                                        "Ashok Leyland Cheetah", "Volvo B9R"]),
                "year": random.randint(2016, 2024),
                "_routes": [], "_max_dist": 0, "_trips": 0,
                "source": "synthetic", "external_ref": None,
            }
            buses.append(doc)
            buses_by_depot[d["depot_id"]].append(doc)

    # ── Trips: expand real departure timings; assign buses round-robin ───────
    trips = []
    rr = defaultdict(int)   # depot_id -> round-robin cursor
    for rt in routes:
        times = parse_times(rt["departure_raw"]) or ["21.00"]
        dep_id = rt["operating_depot_id"]
        fleet = buses_by_depot.get(dep_id) or buses
        duration = int(rt["distance_km"] / 45 * 60)   # ~45 km/h
        seen_times = set()
        for tstr in times:
            if tstr in seen_times:
                continue          # same route cannot depart twice at one time
            seen_times.add(tstr)
            bus = fleet[rr[dep_id] % len(fleet)]
            rr[dep_id] += 1
            trip = {
                "trip_id": stable(id_trip, rt["route_id"], tstr), "route_id": rt["route_id"], "bus_id": bus["bus_id"],
                "route_no": rt["route_no"], "departure_time": tstr,
                "arrival_time": est_arrival(tstr, duration), "duration_min": duration,
                "days_of_week": "DAILY", "source": rt["source"], "external_ref": None,
            }
            trips.append(trip)
            bus["_routes"].append(rt["route_id"])
            bus["_max_dist"] = max(bus["_max_dist"], rt["distance_km"])
            bus["_trips"] += 1
            bus["type"] = rt["type"]   # last route's type; fine for demo

    # ── Intelligent seat capacity (longer routes & busier buses → more seats) ─
    def seat_layout(cap, pattern):
        if pattern == "2+1":
            cols = 3
            rows = -(-cap // cols)     # ceil
            return {"pattern": "2+1", "cols": cols, "rows": rows,
                    "aisle_after": 2, "back_row": 0, "capacity": cap}
        cols = 4
        back = 5 if cap >= 30 else 0
        rows = -(-(cap - back) // cols)
        return {"pattern": "2+2", "cols": cols, "rows": rows,
                "aisle_after": 2, "back_row": back, "capacity": cap}

    for b in buses:
        md = b["_max_dist"]
        busy = b["_trips"] >= 4
        premium = b["type"] in ("A/C", "A/C ULTRA")
        if md > 500:
            if premium:
                cap, pattern = 30, "2+1"          # long-haul A/C sleeper-style
            else:
                cap, pattern = 54, "2+2"          # long-haul seater
        elif md >= 300:
            cap, pattern = (54 if busy else 51), "2+2"
        elif md > 0:
            cap, pattern = (54 if busy else 40), "2+2"
        else:
            cap, pattern = 40, "2+2"              # bus not assigned any route
        b["capacity"] = cap
        b["seat_layout"] = seat_layout(cap, pattern)
        for k in ("_routes", "_max_dist", "_trips"):
            b.pop(k, None)

    # ── Drivers & conductors (~1.3 per bus), per depot ───────────────────────
    def make_person(depot, kind, idx):
        name = f"{random.choice(TAMIL_FIRST)} {random.choice(TAMIL_LAST)}"
        base = {
            "name": name, "employee_no": f"TN{kind[0].upper()}{100000 + idx}",
            "depot_id": depot["depot_id"], "depot_code": depot["depot_code"],
            "phone": f"9{random.randint(100000000, 999999999)}",
            "source": "synthetic", "external_ref": None,
        }
        return base

    drivers, conductors = [], []
    di = ci = 0
    for d in operating_depots:
        n = max(1, int(len(buses_by_depot[d["depot_id"]]) * 1.3))
        for _ in range(n):
            drv = make_person(d, "driver", di); drv["driver_id"] = stable(id_drv, drv["employee_no"])
            drv["license_no"] = f"TN{random.randint(10, 99)}{random.randint(10**9, 10**10 - 1)}"
            drivers.append(drv); di += 1
            con = make_person(d, "conductor", ci); con["conductor_id"] = stable(id_con, con["employee_no"])
            conductors.append(con); ci += 1

    drivers_by_depot = defaultdict(list)
    conductors_by_depot = defaultdict(list)
    for d in drivers:
        drivers_by_depot[d["depot_id"]].append(d)
    for c in conductors:
        conductors_by_depot[c["depot_id"]].append(c)

    # ── Bus schedules: one per trip linking trip+bus+driver+conductor ────────
    bus_by_id = {b["bus_id"]: b for b in buses}
    schedules = []
    for tp in trips:
        dep_id = bus_by_id[tp["bus_id"]]["depot_id"]
        drv = random.choice(drivers_by_depot[dep_id]) if drivers_by_depot[dep_id] else None
        con = random.choice(conductors_by_depot[dep_id]) if conductors_by_depot[dep_id] else None
        schedules.append({
            "schedule_id": stable(id_sched, tp["trip_id"]), "trip_id": tp["trip_id"], "bus_id": tp["bus_id"],
            "driver_id": drv["driver_id"] if drv else None,
            "conductor_id": con["conductor_id"] if con else None,
            "effective_from": "2026-07-01", "status": "active",
            "source": "synthetic", "external_ref": None,
        })

    # ── Persist (idempotent reseed) ──────────────────────────────────────────
    # Upsert on the NATURAL key and reuse existing surrogate ids (see `stable`),
    # so re-running this seeder never orphans live lost/found reports that
    # reference route_id / trip_id / bus_id. $set (rather than a full replace)
    # also preserves runtime-added fields such as a route's cached road_geometry.
    def upsert_all(coll, docs, key_fields, label):
        # Guard against two generated docs sharing one natural key.
        uniq, seen = [], set()
        for d in docs:
            k = tuple(d.get(f) for f in key_fields)
            if k in seen:
                continue
            seen.add(k)
            uniq.append(d)

        # Collapse any pre-existing duplicates FIRST, so each natural key maps to
        # exactly one document (an upsert alone cannot remove a duplicate that
        # shares its key with the doc being updated).
        dup_ids, first_seen = [], set()
        for x in db[coll].find({}, {f: 1 for f in key_fields}):
            k = tuple(x.get(f) for f in key_fields)
            if k in first_seen:
                dup_ids.append(x["_id"])
            else:
                first_seen.add(k)
        if dup_ids:
            db[coll].delete_many({"_id": {"$in": dup_ids}})

        if uniq:
            db[coll].bulk_write(
                [UpdateOne({f: d.get(f) for f in key_fields}, {"$set": d}, upsert=True)
                 for d in uniq],
                ordered=False
            )

        # Converge the collection to what this run generated.
        stale = [x["_id"] for x in db[coll].find({}, {f: 1 for f in key_fields})
                 if tuple(x.get(f) for f in key_fields) not in seen]
        if stale:
            db[coll].delete_many({"_id": {"$in": stale}})

        dropped = len(docs) - len(uniq)
        extra = []
        if dropped:
            extra.append(f"dup keys skipped: {dropped}")
        if dup_ids:
            extra.append(f"existing dupes collapsed: {len(dup_ids)}")
        if stale:
            extra.append(f"stale removed: {len(stale)}")
        print(f"  {label:14} {len(uniq)}" + (f"   ({', '.join(extra)})" if extra else ""))

    print("Seeding collections:")
    upsert_all(T.DEPOTS, depots, ["depot_code"], "depots")
    upsert_all(T.STOPS, stops, ["name"], "stops")
    upsert_all(T.ROUTES, routes, ["route_no", "origin_name", "dest_name"], "routes")
    upsert_all(T.BUSES, buses, ["registration_no"], "buses")
    upsert_all(T.TRIPS, trips, ["route_id", "departure_time"], "trips")
    upsert_all(T.DRIVERS, drivers, ["employee_no"], "drivers")
    upsert_all(T.CONDUCTORS, conductors, ["employee_no"], "conductors")
    upsert_all(T.BUS_SCHEDULES, schedules, ["trip_id"], "bus_schedules")

    # helpful indexes for the API lookups
    db[T.ROUTES].create_index("route_id")
    db[T.ROUTES].create_index([("origin_name", 1), ("dest_name", 1)])
    db[T.TRIPS].create_index("route_id")
    db[T.TRIPS].create_index("trip_id")
    db[T.BUSES].create_index("bus_id")
    db[T.STOPS].create_index("stop_id")

    print("\nDepot home-city mapping (real codes):")
    for code in real_codes:
        print(f"  {code:5} -> {depot_by_code[code]['city']}")
    print("\nDone.")



# ═══ 3. Tickets (mock PNR + mock UIDAI databases) ════════════════════════════

# (pnr, seq, name, phone as typed by the booking system, status, route, days ago)
SAMPLE_BOOKINGS = [
    ("T26100101", 1, "Arun Kumar",          "9876543210",       "confirmed", "SETC 101 Chennai → Coimbatore",   2),
    ("T26100102", 1, "Priya Raman",         "+91 94440 12345",  "confirmed", "SETC 133 Chennai → Madurai",      1),
    ("T26100103", 1, "கார்த்திக் செல்வம்",  "098400-11223",     "confirmed", "SETC 180 Madurai → Tirunelveli",  0),
    ("T26100104", 1, "Lakshmi Narayanan",   "9003312345",       "cancelled", "SETC 101 Chennai → Coimbatore",   3),
    ("T26100105", 1, "Senthil Kumar",       "9123456780",       "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100105", 2, "Meena Senthil",       "91 91234 56780",   "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100105", 3, "Ravi Senthil",        "8098765432",       "confirmed", "SETC 122 Chennai → Trichy",       1),
    ("T26100106", 1, "Kavitha Mani",        "7010203040",       "completed", "SETC 101 Chennai → Coimbatore", 120),
    # The review panel's login (REVIEW_LOGIN): booked yesterday, nothing filed yet.
    ("T26100107", 1, "Divya Prakash",       "9500011122",       "confirmed", "SETC 101 Chennai → Coimbatore",   1),
]

# Contact preferences used by Feature 2 notifications: (pnr, seq) → (on WhatsApp?, email).
# Unlisted passengers: registered phone is on WhatsApp, no email.
CONTACTS = {
    ("T26100101", 1): (True, "arun.kumar@example.com"),
    ("T26100102", 1): (False, "priya.raman@example.com"),    # email only
    ("T26100105", 3): (False, None),                        # no WhatsApp, no email → in-app only
}

# Seat per passenger (Phase 2 journey links).
SEATS = {("T26100101", 1): 12, ("T26100102", 1): 7, ("T26100103", 1): 21, ("T26100104", 1): 3,
         ("T26100105", 1): 14, ("T26100105", 2): 15, ("T26100105", 3): 16, ("T26100106", 1): 9,
         ("T26100107", 1): 18}

# The database itself refuses clear-text bookings: only the PNR index, the
# passenger number, the sealed blob and timestamps may exist on a document.
BOOKINGS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["pnrIndex", "passengerSeq", "sealed"],
        "additionalProperties": False,
        "properties": {
            "_id":          {},
            "pnrIndex":     {"bsonType": "string"},
            "passengerSeq": {"bsonType": ["int", "long"]},
            "sealed":       SEALED_SCHEMA,
            "createdAt":    {"bsonType": "date"},
            "updatedAt":    {"bsonType": "date"},
        },
    }
}


def booking_store(db, signing_secret, vault=None) -> BookingStore:
    """The encrypted PNR store, keyed exactly like the app (same SIGNING_SECRET / data keys)."""
    return BookingStore(db, vault or Vault.from_env(signing_secret))


def seed_bookings(db, now=None, signing_secret=None, vault=None) -> int:
    """
    Upsert the sample bookings (encrypted) into `db`. Returns the rows written.
    `signing_secret` must be the app's SIGNING_SECRET: it keys the idProof
    tokens and, unless DATA_ENCRYPTION_KEYS is set, the encryption itself.
    """
    if not signing_secret:
        raise ValueError("seed_bookings needs the app's SIGNING_SECRET (bookings are encrypted)")
    now = now or utcnow()
    today = today_ist(now)
    store = booking_store(db, signing_secret, vault)
    drop_legacy_indexes(db)
    store.migrate_plaintext()
    for pnr, seq, name, phone, status, route, days_ago in SAMPLE_BOOKINGS:
        whatsapp, email = CONTACTS.get((pnr, seq), (True, None))
        fields = {
            "passengerName":   name,
            "registeredPhone": normalize_phone(phone),
            "status":          status,
            "route":           route,
            "travelDate":      (today - timedelta(days=days_ago)).isoformat(),
            "whatsapp":        whatsapp,
            "email":           email,
            "idProof":         booking_id_proof(signing_secret, name),
        }
        store.upsert(pnr, seq, fields, now=to_db(now))
    return len(SAMPLE_BOOKINGS)


def _route_cities(route_text: str):
    """'SETC 101 Chennai → Coimbatore' → ('chennai', 'coimbatore')."""
    parts = re.sub(r"^\s*SETC\s+\S+\s*", "", route_text or "").split("→")
    if len(parts) != 2:
        return None, None
    return parts[0].strip().lower(), parts[1].strip().lower()


def _find_route(db, origin: str, dest: str):
    """Best transport route for a booking's cities: exact ends, else via, else same origin."""
    rx = lambda c: {"$regex": f"^{re.escape(c)}$", "$options": "i"}   # noqa: E731
    for query in ({"origin_name": rx(origin), "dest_name": rx(dest)},
                  {"origin_name": rx(origin), "stop_cities": dest},
                  {"origin_name": rx(origin)}):
        route = db.routes.find_one(query, sort=[("route_no", 1)])
        if route and len(route.get("stop_sequence") or []) >= 3:
            return route
    return None


def link_bookings_to_transport(db, signing_secret, vault=None) -> int:
    """
    Give every sample booking a real journey in the seeded transport network:
    route, first trip (by departure), board at the origin, alight mid-route (so
    the bus — and any forgotten bag — continues to downstream depots), and a
    seat that fits the bus. No-op (returns 0) when the network is not seeded.
    """
    store = booking_store(db, signing_secret, vault)
    linked = 0
    for pnr, seq, _name, _phone, _status, route_text, _days in SAMPLE_BOOKINGS:
        origin, dest = _route_cities(route_text)
        route = _find_route(db, origin, dest) if origin else None
        if not route:
            continue
        trip = db.trips.find_one({"route_id": route["route_id"]}, sort=[("departure_time", 1), ("trip_id", 1)])
        if not trip:
            continue
        bus = db.buses.find_one({"bus_id": trip.get("bus_id")}) or {}
        seq_ids = route["stop_sequence"]
        seat = SEATS.get((pnr, seq), 10)
        capacity = int(bus.get("capacity") or 40)
        store.upsert(pnr, seq, {
            # The ticket names the bus route it is linked to, so the passenger's
            # dashboard and their reports always show the same route.
            "route": f"SETC {route.get('route_no')} {route.get('origin_name')} → {route.get('dest_name')}",
            "routeId": route["route_id"], "tripId": trip["trip_id"],
            "boardingStopId": seq_ids[0], "alightingStopId": seq_ids[max(1, len(seq_ids) // 2)],
            "seatNo": min(seat, capacity),
        })
        linked += 1
    return linked


def apply_bookings_validator(db) -> None:
    """Lenient validator (moderate level) so the collection behaves like a table."""
    if BOOKINGS in db.list_collection_names():
        db.command("collMod", BOOKINGS, validator=BOOKINGS_VALIDATOR, validationLevel="moderate")
    else:
        db.create_collection(BOOKINGS, validator=BOOKINGS_VALIDATOR, validationLevel="moderate")


# ═══ All of it ═════════════════════════════════════════════════════════════
def seed_all(db, signing_secret, vault=None) -> dict:
    """Steps 1-3 into `db`. `signing_secret` must be the app's SIGNING_SECRET."""
    vault = vault or Vault.from_env(signing_secret)
    logins = seed_depot_logins(db)
    print("Transport network:")
    seed_transport(db)
    rows = seed_bookings(db, signing_secret=signing_secret, vault=vault)
    citizens = seed_mock_uidai(db, signing_secret, vault=vault)
    apply_bookings_validator(db)
    ensure_auth_indexes(db)
    ensure_identity_indexes(db)
    linked = link_bookings_to_transport(db, signing_secret, vault=vault)
    db["seed_info"].replace_one({"_id": "seed"}, {"_id": "seed", "version": SEED_VERSION,
                                                  "seededAt": to_db(utcnow())}, upsert=True)
    print(f"Tickets: {rows} booking rows, {citizens} mock Aadhaar citizens, {linked} linked to real trips.")
    return {"depotLogins": logins, "bookings": rows, "citizens": citizens, "linked": linked}


def main():
    load_dotenv()
    uri = os.environ.get("MONGO_URI")
    secret = (os.environ.get("SIGNING_SECRET") or "").strip()
    if not uri or len(secret) < 32:
        raise SystemExit("Set MONGO_URI and SIGNING_SECRET (32+ chars), or just run `python start.py`.")
    seed_all(MongoClient(uri)[DB_NAME], secret)


if __name__ == "__main__":
    main()
