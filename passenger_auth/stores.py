"""
Encrypted stores for the two mock external systems (see vault.py).

  CitizenStore — the mock UIDAI database (collection `mock_uidai`)
      {aadhaarToken, vidToken, sealed}              tokens are keyed HMACs of the numbers
  BookingStore — the mock TNSTC PNR database (collection `bookings`)
      {pnrIndex, passengerSeq, sealed, createdAt, updatedAt}

Everything personal (names, phones, e-mail, journey, ID proof, booking status)
lives only inside `sealed`. The stores hand the rest of the app plain dicts with
the same field names as before, so callers never touch ciphertext.
"""
import logging

from .vault import VaultError

logger = logging.getLogger(__name__)

MOCK_UIDAI = "mock_uidai"
BOOKINGS = "bookings"

BOOKING_FIELDS = ("passengerName", "registeredPhone", "status", "route", "travelDate", "whatsapp", "email",
                  "idProof", "routeId", "tripId", "boardingStopId", "alightingStopId", "seatNo")
CITIZEN_FIELDS = ("last4", "name", "linkedPhone", "yearOfBirth", "gender", "status")


class CitizenStore:
    def __init__(self, db, vault):
        self.collection = db[MOCK_UIDAI]
        self.vault = vault

    @staticmethod
    def _aad(aadhaar_token):
        return f"uidai|{aadhaar_token}"

    def _open(self, doc):
        if not doc or "sealed" not in doc:
            return None
        try:
            record = self.vault.open("uidai", doc["sealed"], self._aad(doc["aadhaarToken"]))
        except VaultError:
            logger.error("mock_uidai record %s… could not be decrypted (wrong key or tampered).",
                         str(doc.get("aadhaarToken"))[:8])
            return None
        return {**record, "aadhaarToken": doc["aadhaarToken"], "vidToken": doc.get("vidToken")}

    def by_token(self, field: str, token: str, active_only=True):
        """field: 'aadhaarToken' or 'vidToken'."""
        rec = self._open(self.collection.find_one({field: token}))
        if rec and active_only and rec.get("status") != "active":
            return None
        return rec

    def upsert(self, aadhaar_token, vid_token, record: dict):
        record = {k: record[k] for k in CITIZEN_FIELDS if k in record}
        self.collection.update_one(
            {"aadhaarToken": aadhaar_token},
            {"$set": {"vidToken": vid_token, "sealed": self.vault.seal("uidai", record, self._aad(aadhaar_token))},
             "$unset": {k: "" for k in CITIZEN_FIELDS}},            # never leave a clear copy behind
            upsert=True)

    def migrate_plaintext(self) -> int:
        """Seal any record written before encryption existed. Returns how many were migrated."""
        n = 0
        for doc in self.collection.find({"sealed": {"$exists": False}}):
            self.upsert(doc["aadhaarToken"], doc.get("vidToken"), {k: doc.get(k) for k in CITIZEN_FIELDS})
            n += 1
        return n

    def reseal_all(self) -> int:
        """Re-encrypt records still under an old key id (key rotation)."""
        n = 0
        for doc in self.collection.find({"sealed": {"$exists": True}}):
            if self.vault.needs_rotation(doc["sealed"]):
                rec = self._open(doc)
                if rec:
                    self.upsert(doc["aadhaarToken"], doc.get("vidToken"), rec)
                    n += 1
        return n


class BookingStore:
    def __init__(self, db, vault):
        self.collection = db[BOOKINGS]
        self.vault = vault

    def index(self, pnr: str) -> str:
        return self.vault.index("pnr", pnr)

    @staticmethod
    def _aad(pnr_index, seq):
        return f"pnr|{pnr_index}|{int(seq)}"

    def _open(self, doc, pnr=None):
        try:
            record = self.vault.open("pnr", doc["sealed"], self._aad(doc["pnrIndex"], doc["passengerSeq"]))
        except (VaultError, KeyError):
            logger.error("booking record %s… could not be decrypted (wrong key or tampered).",
                         str(doc.get("pnrIndex"))[:8])
            return None
        record = {**record, "passengerSeq": doc["passengerSeq"]}
        if pnr is not None:
            record["pnr"] = pnr
        elif "pnr" in record:
            pass
        return record

    def rows(self, pnr: str) -> list:
        """All passengers on a PNR, decrypted, in passenger order."""
        docs = self.collection.find({"pnrIndex": self.index(pnr)}).sort("passengerSeq", 1)
        return [r for r in (self._open(d, pnr) for d in docs) if r is not None]

    def get(self, pnr: str, seq: int):
        doc = self.collection.find_one({"pnrIndex": self.index(pnr), "passengerSeq": int(seq)})
        return self._open(doc, pnr) if doc else None

    def upsert(self, pnr: str, seq: int, fields: dict, now=None):
        """Merge `fields` into the passenger row (created if missing) and re-seal it."""
        idx = self.index(pnr)
        current = self.get(pnr, seq) or {}
        record = {k: v for k, v in {**current, **fields}.items() if k in BOOKING_FIELDS}
        record["pnr"] = pnr                     # sealed too, so a decrypted row knows its PNR
        update = {"$set": {"sealed": self.vault.seal("pnr", record, self._aad(idx, seq))}}
        if now is not None:
            update["$set"]["updatedAt"] = now
            update["$setOnInsert"] = {"createdAt": now}
        self.collection.update_one({"pnrIndex": idx, "passengerSeq": int(seq)}, update, upsert=True)

    def update(self, pnr: str, fields: dict, seq=None) -> int:
        """Merge `fields` into one passenger (seq) or every passenger on the PNR."""
        targets = [r["passengerSeq"] for r in self.rows(pnr) if seq is None or r["passengerSeq"] == seq]
        for s in targets:
            self.upsert(pnr, s, fields)
        return len(targets)

    def all_rows(self) -> list:
        return [r for r in (self._open(d) for d in self.collection.find({"sealed": {"$exists": True}}))
                if r is not None]

    def migrate_plaintext(self) -> int:
        """Seal bookings written before encryption existed, then drop their clear fields."""
        n = 0
        for doc in list(self.collection.find({"sealed": {"$exists": False}, "pnr": {"$exists": True}})):
            self.upsert(doc["pnr"], doc["passengerSeq"], {k: doc.get(k) for k in BOOKING_FIELDS if k in doc},
                        now=doc.get("updatedAt"))
            self.collection.delete_one({"_id": doc["_id"]})
            n += 1
        return n

    def reseal_all(self) -> int:
        n = 0
        for doc in self.collection.find({"sealed": {"$exists": True}}):
            if self.vault.needs_rotation(doc["sealed"]):
                rec = self._open(doc)
                if rec and rec.get("pnr"):
                    self.upsert(rec["pnr"], doc["passengerSeq"], rec)
                    n += 1
        return n


def drop_legacy_indexes(db) -> None:
    """The old clear-text unique index (pnr, passengerSeq) would collide once `pnr` is gone."""
    try:
        if "pnr_1_passengerSeq_1" in db[BOOKINGS].index_information():
            db[BOOKINGS].drop_index("pnr_1_passengerSeq_1")
    except Exception as e:
        logger.warning("Could not drop the legacy bookings index: %s", e)
