"""
Lenient MongoDB JSON-schema validators for the Phase 2 fields (validationLevel
"moderate", like transport_schema.py): new documents must use the right types,
existing documents are left alone. Applied at startup; failures are logged only.
"""
import logging

from passenger_auth.identity import MOCK_UIDAI, MOCK_UIDAI_VALIDATOR
from seed_bookings import BOOKINGS_VALIDATOR
from vocabulary import CATEGORIES, STORAGE_KEYS

logger = logging.getLogger(__name__)

_IDENTITY = {"bsonType": ["object", "null"], "required": ["type", "token", "last4"],
             "properties": {"type": {"enum": ["aadhaar"]}, "token": {"bsonType": "string"},
                            "last4": {"bsonType": "string", "pattern": "^[0-9]{4}$"}}}

LOST_REPORTS_VALIDATOR = {"$jsonSchema": {"bsonType": "object", "properties": {
    "category":            {"enum": list(CATEGORIES)},
    "storageLocation":     {"enum": sorted(STORAGE_KEYS) + [None]},
    "descriptionOriginal": {"bsonType": ["string", "null"], "maxLength": 1000},
    "descriptionLang":     {"enum": ["en", "ta", "hi"]},
    "detailScore":         {"enum": [0, 25, 50, 75, 100]},
    "ticketType":          {"enum": ["pnr", "general"]},
    "alightingChanged":    {"bsonType": "bool"},
    "ownerIdentity":       _IDENTITY,
}}}

FOUND_REPORTS_VALIDATOR = {"$jsonSchema": {"bsonType": "object", "properties": {
    "category":    {"enum": list(CATEGORIES)},
    "images":      {"bsonType": "array", "items": {"bsonType": "string"}},
    "captureMode": {"enum": ["manual", "camera_scan"]},
}}}

ITEM_DRAFTS_VALIDATOR = {"$jsonSchema": {"bsonType": "object",
                                         "required": ["draftId", "depotPhone", "status", "frames", "closeups"],
                                         "properties": {
    "draftId":    {"bsonType": "string"},
    "depotPhone": {"bsonType": "string"},
    "status":     {"enum": ["capturing", "analysing", "done", "failed"]},
    "frames":     {"bsonType": "object"},
    "closeups":   {"bsonType": "array"},
}}}

VALIDATORS = {"lost_reports": LOST_REPORTS_VALIDATOR, "found_reports": FOUND_REPORTS_VALIDATOR,
              "item_drafts": ITEM_DRAFTS_VALIDATOR, MOCK_UIDAI: MOCK_UIDAI_VALIDATOR,
              "bookings": BOOKINGS_VALIDATOR}       # both mock databases accept sealed records only


def apply_phase2_validators(db) -> None:
    existing = set(db.list_collection_names())
    for name, validator in VALIDATORS.items():
        try:
            if name in existing:
                db.command("collMod", name, validator=validator, validationLevel="moderate")
            else:
                db.create_collection(name, validator=validator, validationLevel="moderate")
        except Exception as e:                    # e.g. mongomock, or no collMod privilege on Atlas
            logger.info("Validator for %s not applied: %s", name, e)
