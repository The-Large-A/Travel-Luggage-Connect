"""
seed_bookings.py — mock TNSTC PNR bookings for passenger OTP login (Feature 1).

Idempotent: each passenger row is upserted on (pnr, passengerSeq), so re-running
refreshes the sample data without duplicating it. Travel dates are relative to
today (IST) so the sample PNRs stay inside the Lost & Found access window.

    python seed_bookings.py

Sample PNRs (all phones are fictitious):
    T26100101  Arun Kumar          +91 98765 43210  confirmed, 2 days ago
    T26100102  Priya Raman         +91 94440 12345  confirmed, yesterday
    T26100103  கார்த்திக் செல்வம்     +91 98400 11223  confirmed, today (Unicode name)
    T26100104  Lakshmi Narayanan   +91 90033 12345  CANCELLED  → never gets an OTP
    T26100105  Senthil Kumar       +91 91234 56780  confirmed, multi-passenger PNR:
               Meena Senthil       +91 91234 56780    (same phone as Senthil)
               Ravi Senthil        +91 80987 65432    (own phone)
    T26100106  Kavitha Mani        +91 70102 03040  completed, 120 days ago → outside window

Contact preferences (Feature 2): Arun has WhatsApp + email, Priya has email only,
Ravi Senthil has neither (in-app notifications only), everyone else WhatsApp only.

Phase 2 additions:
  * idProof — TNSTC records "Identity Type and ID No." on every online booking.
    Every passenger booked with their (mock) Aadhaar, because login is always two
    layers: Aadhaar first, then a PNR booked under that same Aadhaar. Only a
    keyed token and the last 4 digits are kept (see passenger_auth/identity.py).
  * encryption — this is the mock TNSTC PNR database: every row is sealed with
    AES-256-GCM and found by a keyed index of the PNR (passenger_auth/stores.py).
  * journey links — tripId, routeId, boardingStopId, alightingStopId, seatNo per
    passenger, resolved against the seeded transport network when it exists, so
    the report form can fill the journey from the ticket.

Mock Aadhaar numbers for the demo are listed in passenger_auth.identity.MOCK_CITIZENS.
"""
import os
import re
from datetime import timedelta

from dotenv import load_dotenv
from pymongo import MongoClient

from passenger_auth import ensure_auth_indexes
from passenger_auth.identity import SEALED_SCHEMA, booking_id_proof, seed_mock_uidai
from passenger_auth.identity_service import ensure_identity_indexes
from passenger_auth.phone import normalize_phone
from passenger_auth.stores import BOOKINGS, BookingStore, drop_legacy_indexes
from passenger_auth.timeutil import to_db, today_ist, utcnow
from passenger_auth.vault import Vault

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
         ("T26100105", 1): 14, ("T26100105", 2): 15, ("T26100105", 3): 16, ("T26100106", 1): 9}

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


def main():
    load_dotenv()
    uri = os.environ.get("MONGO_URI")
    if not uri:
        print("MONGO_URI not found in environment")
        return
    secret = (os.environ.get("SIGNING_SECRET") or "").strip()
    if len(secret) < 32:
        print("SIGNING_SECRET (32+ chars) must be set: booking ID tokens and the mock UIDAI use it.")
        return
    db = MongoClient(uri)["tn_bus_lost_found"]
    vault = Vault.from_env(secret)
    n = seed_bookings(db, signing_secret=secret, vault=vault)     # also seals any old clear-text rows
    citizens = seed_mock_uidai(db, secret, vault=vault)
    apply_bookings_validator(db)
    ensure_auth_indexes(db)
    ensure_identity_indexes(db)
    linked = link_bookings_to_transport(db, secret, vault=vault)
    print(f"Seeded {n} booking rows across {len({b[0] for b in SAMPLE_BOOKINGS})} PNRs, "
          f"{citizens} mock UIDAI citizens (both encrypted); {linked} rows linked to real trips.")


if __name__ == "__main__":
    main()
