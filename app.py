import os
import re
import math
import uuid
import logging
from datetime import datetime, timedelta

import requests
from flask import (
    Flask, render_template, request,
    redirect, url_for, session, flash, jsonify, g
)
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from pymongo import MongoClient
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv

# Load env variables BEFORE importing modules that depend on them (like Twilio and OCR)
load_dotenv()

import groq

from similarity import TextSimilarity, ImageSimilarity, OCRExtractor
import matching
from notifications import notification_service
import transport_schema as T
from passenger_auth import init_passenger_auth, require_passenger
import description_enhancer
from public_url import LOOPBACK, public_base_url
from passenger_auth.phone import mask_phone, try_normalize_phone
from passenger_auth.timeutil import to_db, utcnow, fmt_ist
from match_notify import init_match_notify, ACTIVE_STATUSES as MATCH_ACTIVE_STATUSES
from match_notify.service import seq_visible
from claims import init_claims, CATEGORIES as ITEM_CATEGORIES, ATTRIBUTES as PRIVATE_ATTRIBUTES
from claims import encode_private_attributes, new_tag_id
from claims.options import build_choices
from vocabulary import (CATEGORY_LABELS, STORAGE_KEYS, STORAGE_LOCATIONS, description_strength,
                        vocabulary_payload)
from item_intake import init_item_intake
from report_schema import apply_phase2_validators

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tn-bus-lost-found-dev-key-2026')

# CSRF protection for all state-changing POST requests (forms + JSON fetches).
csrf = CSRFProtect(app)

# MongoDB
MONGO_URI = os.environ.get('MONGO_URI')
client = MongoClient(MONGO_URI)
db = client['tn_bus_lost_found']

lost_collection    = db['lost_reports']   # Passenger lost item reports
found_collection   = db['found_reports']  # Depot found item reports
matches_collection = db['matches']        # Persisted AI-match links
depots_collection  = db['depots']         # Depot credentials

# Transport master collections (seeded by seed.py, see transport_schema)
stops_collection   = db[T.STOPS]
buses_collection   = db[T.BUSES]
routes_collection  = db[T.ROUTES]
trips_collection   = db[T.TRIPS]


def ensure_indexes():
    """
    Create indexes for the hot query paths (tracking-ID lookup, route/date
    matching scans, depot dashboard). Idempotent; any single failure (e.g. a
    pre-existing duplicate blocking a unique index) is logged, not fatal.
    """
    specs = [
        (lost_collection,    [("tracking_id", 1)], {"unique": True}),
        (lost_collection,    [("route_id", 1), ("date", 1), ("status", 1)], {}),
        (found_collection,   [("depot_phone", 1)], {}),
        (found_collection,   [("route_id", 1), ("date", 1)], {}),
        (matches_collection, [("depot_phone", 1), ("status", 1)], {}),
        (matches_collection, [("request_id", 1)], {}),
        (matches_collection, [("found_id", 1), ("request_id", 1)], {"unique": True}),
    ]
    for coll, keys, opts in specs:
        try:
            coll.create_index(keys, **opts)
        except Exception as e:
            logger.warning(f"Index creation skipped for {coll.name} {keys}: {e}")


ensure_indexes()
apply_phase2_validators(db)

# Feature 1: OTP-based passenger authentication (/auth/* API, /passenger pages,
# and the require_passenger guard used by passenger-facing features).
init_passenger_auth(app, db)

# Feature 2: automatic match notification. The matcher below calls
# match_notifier.record_match(); sending runs on a background worker. Staff
# re-runs reuse the two existing matching directions.
match_notifier = init_match_notify(
    app, db,
    rematch_lost=lambda lost: compute_matches_for_lost(lost),
    rematch_found=lambda found: compute_and_save_matches(
        found, depots_collection.find_one({"phone": found.get("depot_phone")}) or {}),
).service

# Feature 3: secure claims and QR pickup tokens (also makes found-item photos staff-only).
claims_state = init_claims(app, db)
claims_service = claims_state.service

# Phase 2: identity data (the Aadhaar token + last 4 digits on a report) is
# deleted this many days after the case closes. The passenger sees the date.
IDENTITY_PURGE_DAYS = int(os.environ.get('IDENTITY_PURGE_DAYS', '7'))
LOST_FOUND_HELPLINE = (os.environ.get('LOST_FOUND_HELPLINE') or '').strip()
STORAGE_LABELS = {k: label for k, label, _ in STORAGE_LOCATIONS}


def purge_closed_identities(now=None) -> int:
    """
    Two-step retention for identity data on lost reports: when a case closes
    (resolved or cancelled) stamp identityPurgeAt = now + IDENTITY_PURGE_DAYS;
    once that passes, remove ownerIdentity. Runs on the notification worker.
    """
    now = now or utcnow()
    lost_collection.update_many(
        {"ownerIdentity": {"$ne": None}, "status": {"$in": ["resolved", "cancelled"]},
         "identityPurgeAt": None},
        {"$set": {"identityPurgeAt": to_db(now + timedelta(days=IDENTITY_PURGE_DAYS))}})
    res = lost_collection.update_many(
        {"ownerIdentity": {"$ne": None}, "identityPurgeAt": {"$lte": to_db(now)}},
        {"$unset": {"ownerIdentity": ""}, "$set": {"identityPurgedAt": to_db(now)}})
    if res.modified_count:
        logger.info("Purged identity data from %d closed report(s).", res.modified_count)
    return res.modified_count


app.extensions["match_notify"].worker.add_task(purge_closed_identities)

# AI Models
logger.info("Initialising AI models...")
text_sim  = TextSimilarity()
image_sim = ImageSimilarity(db=db)
ocr_ext   = OCRExtractor()
matcher   = matching.Matcher(text_sim, image_sim, image_root="static")
logger.info(f"CLIP available: {image_sim.available}")

# Counter check before a depot hands an item over (security questions; handover_service.py).
from handover_service import HandoverError, HandoverService, resolve_allowed       # noqa: E402
from passenger_auth.ratelimit import RateLimiter                                    # noqa: E402
handover = HandoverService(
    db, "static", image_sim, claims_service.answer_key, claims_state.config,
    use_llm=bool(os.environ.get("GROQ_API_KEY")),
    async_jobs=os.environ.get("HANDOVER_ASYNC", "1") != "0")
security_limiter = RateLimiter(db, utcnow)

# Encrypted QR on lost-report slips (Fernet; tracking_qr.py). The key comes from
# QR_FERNET_KEY, else it is derived from the app's signing secret.
from tracking_qr import QRError, TrackingQR, MAX_AGE_SECONDS as QR_MAX_AGE_SECONDS   # noqa: E402
tracking_qr = TrackingQR({**os.environ, "SIGNING_SECRET": app.extensions["passenger_auth"].config.signing_secret})
QR_FOLDER = os.path.join('static', 'uploads', 'qr')
QR_VERIFY_LIMIT = 30          # scans per IP per 10 minutes


def tracking_qr_url(report) -> str:
    """The URL a phone camera opens: an encrypted token, never the tracking ID itself."""
    token = tracking_qr.issue(report["tracking_id"], report["request_id"])
    return public_base_url() + url_for('verify_report_qr', token=token)     # reachable from a phone


def save_tracking_qr(report):
    """Write the report's QR (SVG) under static/uploads/qr/ and return its relative path."""
    os.makedirs(QR_FOLDER, exist_ok=True)
    name = f"{report['request_id']}.svg"
    with open(os.path.join(QR_FOLDER, name), "w", encoding="utf-8") as fh:
        fh.write(claims_service.qr_svg(tracking_qr_url(report)))
    return f"uploads/qr/{name}"
# Per depot (staff session) and per IP, per 10 minutes.
SECURITY_LIMITS = {"questions": 60, "staff-check": 30, "answers": 20, "override": 10}

# File Upload
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024   # 5 MB
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# SECTION 2 — Static Route & Depot Data
# Stops include lat/lon for the Leaflet map.
ROUTES = [
    {
        "id": "ch-co",
        "name": "Chennai ↔ Coimbatore",
        "color": "#1F6FB2",
        "stops": [
            {"name": "Chennai",      "lat": 13.0827, "lon": 80.2707},
            {"name": "Chengalpattu", "lat": 12.6919, "lon": 79.9765},
            {"name": "Villupuram",   "lat": 11.9390, "lon": 79.4938},
            {"name": "Salem",        "lat": 11.6643, "lon": 78.1460},
            {"name": "Erode",        "lat": 11.3410, "lon": 77.7172},
            {"name": "Coimbatore",   "lat": 11.0168, "lon": 76.9558},
        ]
    },
    {
        "id": "ch-md",
        "name": "Chennai ↔ Madurai",
        "color": "#C62828",
        "stops": [
            {"name": "Chennai",      "lat": 13.0827, "lon": 80.2707},
            {"name": "Chengalpattu", "lat": 12.6919, "lon": 79.9765},
            {"name": "Villupuram",   "lat": 11.9390, "lon": 79.4938},
            {"name": "Trichy",       "lat": 10.7905, "lon": 78.7047},
            {"name": "Dindigul",     "lat": 10.3624, "lon": 77.9695},
            {"name": "Madurai",      "lat":  9.9252, "lon": 78.1198},
        ]
    },
    {
        "id": "md-tn",
        "name": "Madurai ↔ Tirunelveli",
        "color": "#2E7D32",
        "stops": [
            {"name": "Madurai",      "lat":  9.9252, "lon": 78.1198},
            {"name": "Virudhunagar", "lat":  9.5851, "lon": 77.9624},
            {"name": "Kovilpatti",   "lat":  9.1710, "lon": 77.8652},
            {"name": "Tirunelveli",  "lat":  8.7139, "lon": 77.7567},
        ]
    },
]

# SECTION 3 — Helper Functions
def get_route_by_id(route_id: str):
    """Return the route dict for a given route ID, or None."""
    for route in ROUTES:
        if route["id"] == route_id:
            return route
    return None

def get_stop_names(route: dict) -> list:
    """Return a plain list of stop name strings from a route dict."""
    return [s["name"] for s in route["stops"]]

def allowed_file(filename: str) -> bool:
    """True if the file extension is in the allowed set."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_tracking_id() -> str:
    """Return a human-readable tracking ID like TRK-A1B2C3D4."""
    return f"TRK-{uuid.uuid4().hex[:8].upper()}"

def save_uploaded_image(file) -> str:
    """
    Save an uploaded image to the uploads folder.
    Returns relative path like 'uploads/abc.jpg', or None if invalid.
    """
    if file and file.filename and allowed_file(file.filename):
        filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return f"uploads/{filename}"
    return None

def get_depots() -> dict:
    """Load depot info from MongoDB into a dict keyed by phone number."""
    return {d["phone"]: d for d in depots_collection.find()}


# ── Transport DB helpers (used by the /api/transport/* endpoints & matching) ──
def get_route(route_id: str):
    """Return a transport route doc by route_id, or None."""
    if not route_id:
        return None
    return routes_collection.find_one({"route_id": route_id})

def get_trip(trip_id: str):
    """Return a trip doc by trip_id, or None."""
    if not trip_id:
        return None
    return trips_collection.find_one({"trip_id": trip_id})

def get_bus(bus_id: str):
    """Return a bus doc by bus_id, or None."""
    if not bus_id:
        return None
    return buses_collection.find_one({"bus_id": bus_id})

def route_stops(route: dict) -> list:
    """Resolve a route's ordered stop_sequence (ids) into stop docs."""
    seq = route.get("stop_sequence", []) if route else []
    by_id = {s["stop_id"]: s for s in stops_collection.find({"stop_id": {"$in": seq}})}
    return [by_id[sid] for sid in seq if sid in by_id]

def _haversine_km(a, b) -> float:
    """Great-circle distance in km between (lat, lon) tuples a and b."""
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def _parse_seat(value, capacity=None, upper=80):
    """
    Parse a seat number, bounded by the bus capacity when it is known (else a
    sane upper limit). Returns (seat_int, error_message) — error is None on OK.
    """
    try:
        seat = int(str(value).strip())
    except (TypeError, ValueError):
        return None, "Seat number must be a whole number."
    limit = capacity if capacity else upper
    if seat < 1 or seat > limit:
        return None, f"Seat number must be between 1 and {limit}."
    return seat, None


def _parse_travel_date(value):
    """
    Parse a YYYY-MM-DD date and reject anything past tomorrow. The forms cap the date
    client-side, and an unparseable date silently collapses the matching window,
    so it is validated here too. Returns (date_str, error_message).
    """
    try:
        d = datetime.strptime(str(value).strip(), "%Y-%m-%d")
    except (TypeError, ValueError):
        return None, "Please provide a valid date."
    # One day of slack: the browser caps the picker using the CLIENT's local date,
    # so a TN (IST) user just past midnight would otherwise be rejected by a
    # server running on UTC. Anything beyond tomorrow is still refused.
    if d.date() > (datetime.now() + timedelta(days=1)).date():
        return None, "The date cannot be in the future."
    return d.strftime("%Y-%m-%d"), None


def depot_serves_route(depot: dict, route: dict) -> bool:
    """
    True when a depot may log a found item against this route: either it
    operates the service, or the route actually calls at the depot's city (an
    item is handed in wherever the bus reaches).

    Both clauses are needed — an operating depot is not always itself a listed
    stop on its own route, and a few routes have no staffed depot among their
    stops at all. Uses the denormalized `stop_cities` when present, otherwise
    resolves the stop documents.
    """
    if not depot or not route:
        return False
    if route.get("operating_depot_code") and \
       route["operating_depot_code"] == depot.get("depot_code"):
        return True
    city = (depot.get("city") or depot.get("stop") or "").strip().lower()
    if not city:
        return False
    cities = route.get("stop_cities")
    if cities is None:
        cities = [(x.get("city") or "").strip().lower() for x in route_stops(route)]
    return city in cities


def build_seatmap(bus: dict) -> dict:
    """
    Build a seat-map descriptor for the visual picker from a bus's capacity and
    seat_layout. Seat numbers run 1..capacity so the front-end can render a grid.
    """
    layout = (bus or {}).get("seat_layout", {}) or {}
    capacity = int((bus or {}).get("capacity", 0) or 0)
    return {
        "capacity":  capacity,
        "pattern":   layout.get("pattern", "2+2"),
        "cols":      layout.get("cols", 4),
        "rows":      layout.get("rows", 0),
        "aisle_after": layout.get("aisle_after", 2),
        "back_row":  layout.get("back_row", 0),
        "seats":     list(range(1, capacity + 1)),
        "occupied":  [],   # no booking system — picker records where the passenger sat
    }

def luggage_could_be_at_depot(stops: list, src: str, dst: str, depot_stop: str) -> bool:
    """
    Check if a lost item could have reached the depot.

    When a passenger gets off at 'dst', their luggage stays on the bus
    and travels to later stops. So the depot must come AFTER 'dst'.

    Example: src=Chennai, dst=Salem → depot could be Erode or Coimbatore.
    """
    try:
        i = stops.index(src)
        j = stops.index(dst)
        k = stops.index(depot_stop)
        if i < j:
            return k >= j    # Forward journey: depot is at or past destination
        elif i > j:
            return k <= j    # Reverse journey: depot is at or before destination
        else:
            return False    # Same stop — invalid
    except ValueError:
        return False        # Stop not in this route


# SECTION 4 — Matching Engine
def _window_dates(date_str: str, days: int = 7, forward: bool = False) -> list:
    """
    Build a date window around an anchor date (inclusive), as YYYY-MM-DD strings.
    forward=False → [anchor-days … anchor] (a lost item logged up to `days` before
    a found report). forward=True → [anchor … anchor+days] (found reports that may
    appear up to `days` after a passenger's travel date).
    """
    dates = [date_str]
    try:
        d0 = datetime.strptime(date_str, "%Y-%m-%d")
        for i in range(1, days + 1):
            delta = timedelta(days=i)
            d = (d0 + delta) if forward else (d0 - delta)
            dates.append(d.strftime("%Y-%m-%d"))
    except Exception:
        pass
    return dates


def luggage_feasible(found_report: dict, lost: dict, depot: dict) -> bool:
    """
    Blocking rule shared by both matching directions: False when the item
    cannot be at this depot (the bus had not reached the depot's stop after the
    passenger got off) or the route is unknown.
    """
    depot = depot or {}
    transport_route = get_route(found_report.get("route_id"))
    if transport_route:
        seq_ids = transport_route.get("stop_sequence", [])
        depot_city = depot.get("city") or depot.get("stop") or ""
        depot_stop_id = next(
            (s["stop_id"] for s in route_stops(transport_route)
             if s.get("city", "").strip().lower() == depot_city.strip().lower()),
            None
        )
        b, a = lost.get("boarding_stop_id"), lost.get("alighting_stop_id")
        if (b and a and depot_stop_id
                and b in seq_ids and a in seq_ids and depot_stop_id in seq_ids):
            if not luggage_could_be_at_depot(seq_ids, b, a, depot_stop_id):
                return False
        return True
    # Legacy fallback: hardcoded ROUTES + name-based directional logic.
    legacy_route = get_route_by_id(found_report.get("route_id", ""))
    if not legacy_route:
        return False
    names = get_stop_names(legacy_route)
    return luggage_could_be_at_depot(
        names, lost.get("source", ""), lost.get("destination", ""), depot.get("stop", ""))


def _with_ocr(found_report: dict) -> dict:
    """OCR text is cached on the found doc; only a legacy item without it is read live."""
    if found_report.get("ocr_text") is None:
        imgs = found_report.get("images") or ([found_report["image_path"]] if found_report.get("image_path") else [])
        text = ocr_ext.extract_text(os.path.join("static", imgs[0])) if imgs else ""
        found_report = {**found_report, "ocr_text": text or ""}
    return found_report


def score_lost_found(found_report: dict, lost: dict, depot: dict):
    """
    Score a single (found, lost) pair with the v2 evidence matcher (matching.py),
    or None when the luggage filter rules the pair out. Single pairs are
    calibrated against the model's null distribution; the matching passes below
    score whole blocks, which calibrates against the other candidates too.
    """
    if not luggage_feasible(found_report, lost, depot):
        return None
    return matcher.score(_with_ocr(found_report), lost)


def _save_match(found_report: dict, lost: dict, score: dict) -> bool:
    """
    Persist a qualifying (found, lost) pair through Feature 2, which upserts the
    match (unique on reportId+itemId), routes it by confidence (auto-notify /
    staff review / reject) and queues privacy-safe notifications on the
    background worker. Duplicate triggers are no-ops. Returns True if a new match
    document was created.
    """
    match, is_new = match_notifier.record_match(found_report, lost, score)
    if is_new and match is not None:
        handover.pregenerate(match["_id"])          # questions ready before staff press resolve
    return is_new


def _open_lost_near(route_id, date, forward: bool) -> list:
    return list(lost_collection.find({
        "route_id": route_id, "date": {"$in": _window_dates(date or "", forward=forward)},
        "status": {"$nin": ["resolved", "cancelled"]}}))


def _found_near(route_id, date, forward: bool) -> list:
    return list(found_collection.find({
        "route_id": route_id, "date": {"$in": _window_dates(date or "", forward=forward)}}))


def _save_block(block, pairs_of) -> int:
    """Persist every pair of a scored block worth recording; returns how many."""
    n = 0
    for cand, score in block:
        found_report, lost = pairs_of(cand)
        if matching.route(score, match_notifier.config.auto_notify_min,
                          match_notifier.config.review_min) is not None:
            _save_match(found_report, lost, score)
            n += 1
    return n


def compute_and_save_matches(found_report: dict, depot: dict) -> int:
    """
    FOUND-triggered matching: score a new found item against the open lost
    reports on the SAME route within the date window, as ONE block (so the
    similarities are calibrated against each other and the best candidate must
    clearly lead to be auto-notified). Returns how many pairs were recorded.
    """
    found_report = _with_ocr(found_report)
    candidates = [lo for lo in _open_lost_near(found_report.get("route_id"), found_report.get("date"), False)
                  if luggage_feasible(found_report, lo, depot)]
    if not candidates:
        return 0
    block = matcher.score_block(found_report, candidates, query_is_found=True)
    top_lost, top = block[0]
    if top["autoEligible"]:
        # Mutual best: is another found item just as likely to be this passenger's?
        rivals = [_with_ocr(f) for f in _found_near(top_lost.get("route_id"), top_lost.get("date"), True)
                  if f.get("found_id") != found_report.get("found_id") and f.get("status") in (None, "open")]
        if matching.is_contested(matcher, top_lost, found_report, rivals, anchor_is_found=False):
            top["autoEligible"] = False
            top["blockers"].append("another found item is almost as likely to be this passenger's")
    return _save_block(block, lambda lost: (found_report, lost))


def compute_matches_for_lost(lost_report: dict) -> int:
    """
    LOST-triggered matching (symmetric counterpart): score a new lost report
    against the found items already logged on the SAME route within the date
    window, as one block. Closes the gap where a passenger reporting AFTER the
    depot logged the item would otherwise never be matched.
    """
    depot_cache = {}

    def depot_of(found):
        phone = found.get("depot_phone")
        if phone not in depot_cache:
            depot_cache[phone] = depots_collection.find_one({"phone": phone}) or {}
        return depot_cache[phone]
    candidates = [_with_ocr(f) for f in _found_near(lost_report.get("route_id"), lost_report.get("date"), True)
                  if luggage_feasible(f, lost_report, depot_of(f))]
    if not candidates:
        return 0
    block = matcher.score_block(lost_report, candidates, query_is_found=False)
    top_found, top = block[0]
    if top["autoEligible"]:
        rivals = [lo for lo in _open_lost_near(top_found.get("route_id"), top_found.get("date"), False)
                  if lo.get("request_id") != lost_report.get("request_id")]
        if matching.is_contested(matcher, top_found, lost_report, rivals, anchor_is_found=True):
            top["autoEligible"] = False
            top["blockers"].append("another passenger's report is almost as likely")
    return _save_block(block, lambda found: (found, lost_report))


def get_matches_for_depot(depot_phone: str) -> list:
    """
    Load all non-resolved matches for this depot from MongoDB.
    Enriches each match with the full lost_report and found_report data.
    Groups them by found_id for display.
    """
    # Fetch only pending matches belonging to this depot
    raw_matches = list(matches_collection.find({
        "depot_phone": depot_phone,
        "status":      {"$in": list(MATCH_ACTIVE_STATUSES)}
    }).sort("created_at", -1))

    if not raw_matches:
        return []

    # Collect all unique found_ids and request_ids for batch fetching
    found_ids   = list({m["found_id"]   for m in raw_matches})
    request_ids = list({m["request_id"] for m in raw_matches})

    # Batch fetch from DB (much faster than one query per match)
    found_map = {
        f["found_id"]: f
        for f in found_collection.find({"found_id": {"$in": found_ids}})
    }
    lost_map = {
        l["request_id"]: l
        for l in lost_collection.find({"request_id": {"$in": request_ids}})
    }

    # Group matches by found_id
    groups = {}
    for match in raw_matches:
        fid  = match["found_id"]
        rid  = match["request_id"]
        lost = lost_map.get(rid)
        if not lost:
            continue   # Lost report deleted — skip

        # Attach score and match_id to the lost report dict for the template
        enriched_lost = dict(lost)
        enriched_lost["score"]    = match["score"]
        enriched_lost["match_id"] = str(match["_id"])
        enriched_lost["match_status"] = match.get("status")

        if fid not in groups:
            groups[fid] = {
                "found_report": found_map.get(fid, {}),
                "matches":      []
            }
        groups[fid]["matches"].append(enriched_lost)

    # Sort each group's matches by score descending
    result = list(groups.values())
    for group in result:
        group["matches"].sort(key=lambda x: x["score"]["final"], reverse=True)

    return result



# SECTION 5 — Passenger Routes
@app.route('/')
def index():
    """Passenger homepage: report form + Leaflet map."""
    last_tracking_id = session.pop('last_tracking_id', None)
    last_qr = session.pop('last_qr', None)
    return render_template('index.html', last_tracking_id=last_tracking_id, last_qr=last_qr,
                           storage_locations=STORAGE_LOCATIONS, category_labels=CATEGORY_LABELS)


# ── Phase 2: journey from the ticket (PNR autofill) ──────────────────────────
def booking_rows_for(passenger) -> list:
    """The booking rows this session may report for: same PNR + phone, and only
    the verified passenger when the session came from an Aadhaar login."""
    store = app.extensions["passenger_auth"].bookings          # encrypted mock PNR database
    rows = [r for r in store.rows(passenger.pnr)
            if try_normalize_phone(r.get("registeredPhone")) == passenger.phone]
    if passenger.passenger_seqs:
        rows = [r for r in rows if r.get("passengerSeq") in passenger.passenger_seqs]
    return rows


def journey_from_booking(row):
    """Resolve a booking row's trip links into a journey, or None if unlinked/invalid."""
    route = get_route((row or {}).get("routeId"))
    trip = get_trip((row or {}).get("tripId"))
    if not route or not trip or trip.get("route_id") != route["route_id"]:
        return None
    seq = route.get("stop_sequence", [])
    b, a = row.get("boardingStopId"), row.get("alightingStopId")
    if b not in seq or a not in seq or a == b:
        return None
    bus = get_bus(trip.get("bus_id")) or {}
    names = {s["stop_id"]: s["name"] for s in route_stops(route)}
    return {
        "route": {"route_id": route["route_id"], "route_no": route.get("route_no"),
                  "origin_name": route.get("origin_name"), "dest_name": route.get("dest_name"),
                  "distance_km": route.get("distance_km"), "type": route.get("type"), "via": route.get("via", "")},
        "tripId": trip["trip_id"], "departureTime": trip.get("departure_time"),
        "registrationNo": bus.get("registration_no"), "date": row.get("travelDate"),
        "boardingStopId": b, "boardingStop": names.get(b, ""),
        "alightingStopId": a, "alightingStop": names.get(a, ""), "seatNo": row.get("seatNo"),
    }


# The dashboard shows the same journey the report form and the reports use.
app.extensions['passenger_journeys'] = lambda p: [j for j in map(journey_from_booking, booking_rows_for(p)) if j]


@app.route('/api/passenger/journey')
@require_passenger
def api_passenger_journey():
    """The journeys on the logged-in passenger's ticket, for one-click autofill."""
    out = []
    for row in booking_rows_for(g.passenger):
        out.append({"passengerSeq": row.get("passengerSeq"), "passengerName": row.get("passengerName"),
                    "journey": journey_from_booking(row)})
    return jsonify({"ok": True, "pnr": g.passenger.pnr, "passengers": out})


@app.route('/api/vocabulary')
def api_vocabulary():
    """Shared item vocabulary for the readiness meter, category and storage pickers."""
    resp = jsonify(vocabulary_payload())
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route('/lost', methods=['POST'])
@require_passenger(page=True)
def submit_lost():
    """
    Accept a lost luggage report from an OTP-verified passenger (Feature 1).
    The report is owned by the session's (pnr, phone); name and phone come from
    the verified session, never from the form.

    Phase 2: when the ticket is linked to a real trip, route / bus / date /
    boarding stop / seat come from the booking and the form values are ignored
    (they cannot be tampered with). Only the alighting stop may differ — people
    get off early — and that change is recorded.
    """
    passenger = g.passenger
    route_id          = request.form.get('route_id')
    trip_id           = request.form.get('trip_id')
    seat_no           = request.form.get('seat_no')
    boarding_stop_id  = request.form.get('boarding_stop_id')
    alighting_stop_id = request.form.get('alighting_stop_id')
    date              = request.form.get('date')
    description       = (request.form.get('description') or '').strip()
    phone             = passenger.phone
    name              = passenger.passenger_name
    image_file        = request.files.get('image')

    rows = booking_rows_for(passenger)
    seq_raw = request.form.get('passenger_seq')
    row = next((r for r in rows if str(r.get("passengerSeq")) == str(seq_raw)), rows[0] if rows else None)
    journey = journey_from_booking(row) if row else None
    autofilled, alighting_changed = [], False
    if row and row.get("passengerName"):
        name = row["passengerName"]
    if journey:
        route_id, trip_id = journey["route"]["route_id"], journey["tripId"]
        date, boarding_stop_id = journey["date"], journey["boardingStopId"]
        seat_no = journey["seatNo"] or seat_no            # a ticket may not carry a seat: the passenger picks it
        alighting_stop_id = alighting_stop_id or journey["alightingStopId"]
        alighting_changed = alighting_stop_id != journey["alightingStopId"]
        autofilled = ["route", "trip", "date", "boarding"] + (["seat"] if journey["seatNo"] else [])

    if not all([route_id, trip_id, seat_no, boarding_stop_id,
                alighting_stop_id, date, description, phone, name]):
        flash('Please fill in all required fields.', 'error')
        return redirect(url_for('index'))

    route = get_route(route_id)   # transport route
    if not route:
        flash('Invalid route selected.', 'error')
        return redirect(url_for('index'))

    seq = route.get("stop_sequence", [])
    if boarding_stop_id not in seq or alighting_stop_id not in seq:
        flash('Invalid boarding or alighting stop for selected route.', 'error')
        return redirect(url_for('index'))

    if boarding_stop_id == alighting_stop_id:
        # The directional luggage filter can never satisfy src == dst, so such a
        # report would sit unmatchable forever. Reject it up front.
        flash('Boarding and alighting stops must be different.', 'error')
        return redirect(url_for('index'))

    if journey and seq.index(alighting_stop_id) < seq.index(boarding_stop_id):
        flash('You can only get off at a stop after the one you boarded at.', 'error')
        return redirect(url_for('index'))

    date, err = _parse_travel_date(date)
    if err:
        flash(err, 'error')
        return redirect(url_for('index'))

    # The trip must exist AND actually run on the selected route — trip_id is
    # client-supplied and drives the exact-trip match signal.
    trip = get_trip(trip_id)
    if not trip or trip.get("route_id") != route_id:
        flash('The selected bus/service does not run on that route.', 'error')
        return redirect(url_for('index'))

    bus            = get_bus(trip.get("bus_id"))
    bus_id         = trip.get("bus_id")
    departure_time = trip.get("departure_time")

    seat_no, err = _parse_seat(seat_no, (bus or {}).get("capacity"))
    if err:
        flash(err, 'error')
        return redirect(url_for('index'))
    if len(description) > 2000:
        flash('Please keep the description under 2000 characters.', 'error')
        return redirect(url_for('index'))

    stops_map = {s["stop_id"]: s["name"] for s in route_stops(route)}

    tracking_id = generate_tracking_id()
    request_id  = uuid.uuid4().hex   # Internal unique key for DB relations

    image_path = None
    if image_file and image_file.filename:
        image_path = save_uploaded_image(image_file)

    # Phase 2 report details.
    category = request.form.get('category') or 'other'
    if category not in ITEM_CATEGORIES:
        category = 'other'
    storage_location = request.form.get('storage_location') or None
    if storage_location not in STORAGE_KEYS:
        storage_location = None
    description_original = (request.form.get('description_original') or '').strip()[:1000] or None
    description_lang = request.form.get('description_lang')
    description_lang = description_lang if description_lang in ('ta', 'hi') and description_original else 'en'
    owner_identity = ({"type": "aadhaar", "token": passenger.id_token, "last4": passenger.id_last4}
                      if passenger.auth_method == "aadhaar" and passenger.id_token else None)

    report = {
        "request_id":        request_id,    # Unique internal ID (used in matches)
        "tracking_id":       tracking_id,   # Human-readable ID shown to passenger
        "route_id":          route_id,
        "route_no":          route.get("route_no"),
        "route_name":        f"{route.get('origin_name')} → {route.get('dest_name')}",
        "trip_id":           trip_id,
        "bus_id":            bus_id,
        "bus_registration":  (bus or {}).get("registration_no"),
        "departure_time":    departure_time,
        "seat_no":           seat_no,
        "boarding_stop_id":  boarding_stop_id,
        "alighting_stop_id": alighting_stop_id,
        "date":              date,
        # Legacy display fields (keep status page & templates working unchanged)
        "source":            stops_map.get(boarding_stop_id, ""),
        "destination":       stops_map.get(alighting_stop_id, ""),
        "description":       description,
        # A cleaned copy for the text matcher only (never replaces the original; None if
        # no LLM is configured or the rewrite would have added a fact).
        "descriptionEnhanced": description_enhancer.enhance(description),
        "phone":             phone,
        "name":              name,
        "ownerPnr":          passenger.pnr,     # object-level authorization (Feature 2)
        "ownerPhone":        passenger.phone,
        "image_path":        image_path,
        "status":            "pending",     # pending | matched | resolved | cancelled
        "matched_depot":     None,
        "matched_at":        None,
        "created_at":        datetime.now().isoformat(),
        # Phase 2
        "category":            category,
        "storageLocation":     storage_location,
        "descriptionOriginal": description_original,   # e.g. the spoken Tamil, kept beside the English
        "descriptionLang":     description_lang,
        "detailScore":         description_strength(description, category)["score"],
        "ticketType":          "pnr",
        "passengerSeq":        (row or {}).get("passengerSeq"),
        "autofilled":          autofilled,
        "alightingChanged":    alighting_changed,
        "ownerIdentity":       owner_identity,          # token + last 4 only; purged after the case closes
    }

    lost_collection.insert_one(report)

    # Symmetric matching: check against found items already logged on this route.
    # The count is not shown: review-band matches must stay invisible until approved.
    compute_matches_for_lost(report)

    msg = (f'Report submitted! Your Tracking ID is {tracking_id} — '
           f'save it to check your claim status. We will notify you if a depot finds a likely match.')
    flash(msg, 'success')
    session['last_tracking_id'] = tracking_id
    try:                                   # an encrypted QR the passenger can save (72 h; the slip reissues it)
        qr_path = save_tracking_qr(report)
        lost_collection.update_one({"request_id": report["request_id"]},
                                   {"$set": {"qrImagePath": qr_path, "qrIssuedAt": to_db(utcnow())}})
        session['last_qr'] = qr_path
    except Exception:
        logger.exception("Could not create the QR for report %s.", tracking_id)
    return redirect(url_for('index'))


@app.route('/status')
def status_page():
    """Passenger self-service status check page."""
    return render_template('status.html')


# SECTION 6 — Depot Staff Routes
@app.route('/depot-login')
def depot_login_page():
    return render_template('depot_login.html', depots=get_depots())


@app.route('/depot/login', methods=['POST'])
def depot_login():
    phone    = request.form.get('phone')
    password = request.form.get('password')
    depots   = get_depots()

    if phone in depots and depots[phone]["password"] == password:
        session['depot_phone'] = phone
        session['depot_name']  = depots[phone]["name"]
        flash(f'Welcome, {depots[phone]["name"]}!', 'success')
        return redirect(url_for('depot_dashboard'))

    flash('Invalid phone number or password.', 'error')
    return redirect(url_for('depot_login_page'))


@app.route('/depot/logout')
def depot_logout():
    session.pop('depot_phone', None)
    session.pop('depot_name',  None)
    flash('Logged out successfully.', 'success')
    return redirect(url_for('depot_login_page'))


@app.route('/depot')
def depot_dashboard():
    """Depot dashboard: register found items + view AI matches from DB."""
    if 'depot_phone' not in session:
        flash('Please login to access depot dashboard.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_phone = session['depot_phone']
    depots      = get_depots()
    depot       = depots.get(depot_phone)

    if not depot:
        session.clear()
        flash('Invalid depot session.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_found = list(found_collection.find(
        {"depot_phone": depot_phone}
    ).sort("created_at", -1))

    # Load matches from DB (not recomputed — already populated at submit_found time)
    all_matches = get_matches_for_depot(depot_phone)

    return render_template(
        'depot.html',
        depot=depot,
        depot_phone=depot_phone,
        found_reports=depot_found,
        all_matches=all_matches,
        clip_available=image_sim.available,
        storage_labels=STORAGE_LABELS,
    )


class FoundReportError(ValueError):
    """A found-item submission was refused; the message is shown to staff."""


def create_found_report(depot: dict, data: dict, image_paths=(), raw_attributes=None,
                        attribute_choices=None, extra=None):
    """
    Validate and store a found item, log custody, and run matching. Shared by
    the manual depot form and the Phase 2 smart-intake camera scan, so both
    produce identical records. Returns (report, number_of_matches).

    data: route_id, trip_id, date, notes, public_description, category, found_seat_area.
    raw_attributes: plain private answers — hashed here, never stored in clear.
    attribute_choices: {attribute: [option labels]} for multiple-choice questions.
    extra: additional fields (e.g. captureMode, attributes, draftId, ocr_text).
    """
    route_id = data.get('route_id')
    trip_id = data.get('trip_id') or None
    date = data.get('date')
    notes = data.get('notes')
    public_description = (data.get('public_description') or '').strip()
    category = data.get('category') or 'other'
    found_seat_area = data.get('found_seat_area') or None

    if not all([route_id, date, notes, public_description]):
        raise FoundReportError('Please fill in all required fields.')
    route = get_route(route_id)   # transport route
    if not route:
        raise FoundReportError('Invalid route selected.')
    if not depot_serves_route(depot, route):
        raise FoundReportError('This depot does not serve the selected route.')
    date, err = _parse_travel_date(date)
    if err:
        raise FoundReportError(err)

    # Optional trip → carries the bus the item came off, for exact matching.
    # When supplied it must actually run on the selected route (it drives the
    # exact-trip signal). bus_id is derived from the trip only — never read from
    # the form, which would let a client forge the same-bus score boost.
    trip = None
    if trip_id:
        trip = get_trip(trip_id)
        if not trip or trip.get("route_id") != route_id:
            raise FoundReportError('The selected bus/service does not run on that route.')
    bus_id = trip.get("bus_id") if trip else None

    if found_seat_area is not None:
        capacity = (get_bus(bus_id) or {}).get("capacity") if bus_id else None
        found_seat_area, err = _parse_seat(found_seat_area, capacity)
        if err:
            raise FoundReportError(err)

    image_paths = [p for p in image_paths if p]
    image_path = image_paths[0] if image_paths else None
    extra = dict(extra or {})

    # Pre-compute CLIP embeddings + OCR text now so BOTH matching directions are
    # fast later and never re-hit the OCR API for these images.
    if image_sim.available:
        for p in image_paths:
            image_sim.embed(os.path.join("static", p))
    ocr_text = extra.pop("ocr_text", None)
    if ocr_text is None:
        ocr_text = ocr_ext.extract_text(os.path.join("static", image_path)) if image_path else ""

    found_id = uuid.uuid4().hex   # Unique internal ID for this found report

    # Feature 3: private attributes are hashed right here; the raw answers are never stored.
    if category not in ITEM_CATEGORIES:
        category = 'other'
    # Questions are asked for the category the camera was sure of ("other" if it
    # was not); staff-typed items use the category staff picked.
    question_category = extra.get("questionCategory") or category
    raw_attributes = {k: v for k, v in (raw_attributes or {}).items() if isinstance(v, str) and v.strip()}
    if attribute_choices is None:
        # Typed by staff: colour answers become multiple choice with contrasting
        # options (an owner remembering "navy" must not face navy/black/blue).
        raw_attributes, attribute_choices = build_choices(
            raw_attributes, question_category, {k: a["kind"] for k, a in PRIVATE_ATTRIBUTES.items()},
            colours_only=True)
    private_attributes = encode_private_attributes(
        raw_attributes, question_category, public_description, claims_service.answer_key,
        claims_state.config.fuzzy_min_token_length, choices=attribute_choices)

    report = {
        "found_id":        found_id,          # Unique internal ID (used in matches)
        "id":              found_id[:8],      # Short display ID
        "depot_phone":     depot["phone"],
        "depot_name":      depot["name"],
        "route_id":        route_id,
        "route_no":        route.get("route_no"),
        "route_name":      f"{route.get('origin_name')} → {route.get('dest_name')}",
        "trip_id":         trip_id,
        "bus_id":          bus_id,
        "departure_time":  trip.get("departure_time") if trip else None,
        "found_seat_area": found_seat_area,
        "date":            date,
        "notes":           notes,             # private: staff only
        "notesEnhanced":   description_enhancer.enhance(notes),   # cleaned copy for the text matcher
        "public_description": public_description[:120],   # the only text passengers see
        "category":        category,
        "private_attributes": private_attributes,          # hashed claim-verification answers
        "qrTagId":         new_tag_id(),                   # printed and stuck on the item
        "image_path":      image_path,
        "images":          image_paths,       # every photo (smart intake keeps several keyframes)
        "ocr_text":        ocr_text,          # cached; reused by the matcher
        "status":          "open",
        "created_at":      datetime.now().isoformat()
    }
    report.update(extra)

    found_collection.insert_one(report)
    claims_service.record_item_logged(report, depot["phone"])

    # Run AI matching and SAVE results to matches collection
    return report, compute_and_save_matches(report, depot)


@app.route('/depot/found', methods=['POST'])
def submit_found():
    """Accept a found luggage report from depot staff (manual form) and run AI matching."""
    if 'depot_phone' not in session:
        flash('Please login to submit found reports.', 'error')
        return redirect(url_for('depot_login_page'))

    depot_phone = session['depot_phone']
    depots      = get_depots()
    depot       = depots.get(depot_phone)

    if not depot:
        flash('Invalid depot session.', 'error')
        return redirect(url_for('depot_login_page'))

    image_path = None
    if 'image' in request.files:
        image_path = save_uploaded_image(request.files['image'])

    try:
        _, n = create_found_report(
            depot,
            {k: request.form.get(k) for k in ('route_id', 'trip_id', 'date', 'notes', 'public_description',
                                              'category', 'found_seat_area')},
            image_paths=[image_path] if image_path else [],
            raw_attributes={name: request.form.get(f'attr_{name}') for name in PRIVATE_ATTRIBUTES},
            extra={"captureMode": "manual"})
    except FoundReportError as e:
        if image_path:                       # do not leave an orphan upload behind
            try:
                os.remove(os.path.join("static", image_path))
            except OSError:
                pass
        flash(str(e), 'error')
        return redirect(url_for('depot_dashboard'))

    if n > 0:
        flash(f'Found report submitted! {n} potential match(es) found!', 'success')
    else:
        flash('Found report submitted. No matches yet.', 'success')

    return redirect(url_for('depot_dashboard'))



# SECTION 7 — API Endpoints

@app.route('/api/status/<tracking_id>')
def api_status(tracking_id: str):
    """
    Return JSON status of a lost report by tracking ID.
    Used by the passenger 'Check Status' page.
    """
    tracking_id = tracking_id.strip().upper()
    report = lost_collection.find_one({"tracking_id": tracking_id})
    if not report:
        return jsonify({"found": False, "message": "Tracking ID not found."}), 404

    # Privacy (Phase 2): anyone holding a tracking ID — e.g. whoever sees a
    # printed loss slip — gets the status only, never the passenger's name or
    # item description. The owner sees those after logging in.
    return jsonify({
        "found":         True,
        "tracking_id":   report.get("tracking_id"),
        "status":        report.get("status", "pending"),
        "route_name":    report.get("route_name"),
        "date":          report.get("date"),
        "matched_depot": report.get("matched_depot"),
        "matched_at":    report.get("matched_at"),
        "created_at":    report.get("created_at")
    })


def _owned_report(tracking_id: str, passenger):
    report = lost_collection.find_one({"tracking_id": (tracking_id or "").strip().upper()})
    if not report or report.get("ownerPnr") != passenger.pnr or report.get("ownerPhone") != passenger.phone:
        return None
    if not seq_visible(report.get("passengerSeq"), passenger):
        return None                                  # group booking: only this passenger's own reports
    return report


@app.route('/passenger/reports/<tracking_id>/slip')
@require_passenger(page=True)
def loss_slip(tracking_id):
    """Printable loss acknowledgment slip with a QR that opens the public verify page."""
    report = _owned_report(tracking_id, g.passenger)
    if not report:
        return render_template('passenger_not_found.html'), 404
    verify_url = tracking_qr_url(report)          # encrypted; a fresh 72-hour token every time it is printed
    stops = {s["stop_id"]: s["name"] for s in route_stops(get_route(report.get("route_id")) or {})}
    return render_template(
        'loss_slip.html', report=report, verify_url=verify_url,
        qr_svg=claims_service.qr_svg(verify_url),
        storage_label=STORAGE_LABELS.get(report.get("storageLocation")),
        category_label=CATEGORY_LABELS.get(report.get("category") or "other", ("", ""))[0],
        boarding=stops.get(report.get("boarding_stop_id"), report.get("source")),
        alighting=stops.get(report.get("alighting_stop_id"), report.get("destination")),
        issued_at=fmt_ist(utcnow()))


@app.route('/verify/<tracking_id>')
def verify_report(tracking_id):
    """Public check that a loss slip is genuine: status, route and date only."""
    report = lost_collection.find_one({"tracking_id": (tracking_id or "").strip().upper()})
    return render_template('verify_report.html', report=report, tracking_id=tracking_id.upper()[:20]), \
        (200 if report else 404)


def _qr_rate_limited():
    ip = request.remote_addr or 'unknown'
    count, retry = security_limiter.recent_events("qr-verify", ip, 600)
    if count >= QR_VERIFY_LIMIT:
        logger.warning("QR verify rate limit hit.")
        return retry or 60
    security_limiter.record_event("qr-verify", ip, 600)
    return 0


def _qr_status(data) -> dict:
    """What a valid QR may tell anyone holding the slip: status only, no names or descriptions."""
    report = lost_collection.find_one({"request_id": data["r"], "tracking_id": data["t"]}) or {}
    if not report:
        return {}
    claim = claims_service.claims.find_one({"reportId": report["request_id"]}, sort=[("createdAt", -1)]) or {}
    return {"trackingId": report.get("tracking_id"), "status": report.get("status", "pending"),
            "routeName": report.get("route_name"), "date": report.get("date"),
            "claimStatus": claim.get("status"), "issuedAt": data["ts"],
            "expiresAt": data["ts"] + QR_MAX_AGE_SECONDS}


@app.route('/verify/q/<path:token>')
def verify_report_qr(token):
    """Page a phone opens from the encrypted QR: decrypts it, checks its age, shows the status."""
    if _qr_rate_limited():
        return render_template('verify_report.html', report=None, tracking_id='', qr_error='rate_limited'), 429
    try:
        data = tracking_qr.read(token)
    except QRError as e:
        logger.info("QR scan rejected: %s.", e.reason)
        return render_template('verify_report.html', report=None, tracking_id='', qr_error=e.reason), \
            (410 if e.reason == "expired" else 400)
    report = lost_collection.find_one({"request_id": data["r"], "tracking_id": data["t"]})
    return render_template('verify_report.html', report=report, tracking_id=data["t"], qr_error=None), \
        (200 if report else 404)


@app.route('/api/qr/verify', methods=['POST'])
def api_qr_verify():
    """Scanned QR payload (the token or the whole URL) → decrypted, age-checked, status."""
    retry = _qr_rate_limited()
    if retry:
        return jsonify({"valid": False, "reason": "rate_limited", "retryAfterSeconds": retry}), 429
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    if not isinstance(payload, str) or not payload.strip():
        return jsonify({"valid": False, "reason": "malformed"}), 400
    token = payload.strip().rsplit("/verify/q/", 1)[-1]
    try:
        data = tracking_qr.read(token)
    except QRError as e:
        logger.info("QR verify rejected: %s.", e.reason)
        return jsonify({"valid": False, "reason": e.reason}), (410 if e.reason == "expired" else 400)
    status = _qr_status(data)
    if not status:
        return jsonify({"valid": False, "reason": "not_found"}), 404
    return jsonify({"valid": True, **status})


@app.route('/api/transport/routes/<route_id>/downstream')
def api_route_downstream(route_id):
    """
    Where a forgotten item can travel (Phase 2 luggage-path map). The bus keeps
    going after the passenger alights, so the item can only be at the alighting
    stop or later in the direction of travel. Returns the travelled stops, the
    downstream stops, the depots along the downstream part (with public contact
    numbers) and the route's terminus and home depot for the 72-hour timeline.
    """
    route = get_route(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404
    stops = route_stops(route)
    ids = [s["stop_id"] for s in stops]
    alighting = request.args.get('alighting')
    if alighting not in ids:
        return jsonify({"error": "alighting must be a stop on this route"}), 400
    boarding = request.args.get('boarding')
    i, j = (ids.index(boarding) if boarding in ids else 0), ids.index(alighting)
    forward = i <= j
    travelled = stops[i:j + 1] if forward else stops[j:i + 1][::-1]
    downstream = stops[j:] if forward else stops[:j + 1][::-1]

    def stop_view(s):
        return {"stop_id": s["stop_id"], "name": s["name"], "lat": s.get("lat"), "lon": s.get("lon")}

    def depot_view(d, role):
        return {"name": d.get("name"), "city": d.get("city"), "lat": d.get("lat"), "lon": d.get("lon"),
                "publicPhone": d.get("publicPhone"), "whatsapp": d.get("whatsapp"), "role": role}

    depots, seen = [], set()
    for s in downstream:
        city = (s.get("city") or "").strip()
        if not city:
            continue
        d = depots_collection.find_one({"city": {"$regex": f"^{re.escape(city)}$", "$options": "i"}})
        if d and d.get("phone") not in seen:
            seen.add(d.get("phone"))
            depots.append(depot_view(d, "downstream"))
    terminus = depots[-1] if depots else None
    home = depots_collection.find_one({"depot_code": route.get("operating_depot_code")}) \
        if route.get("operating_depot_code") else None
    return jsonify({
        "route_id": route_id, "direction": "forward" if forward else "reverse",
        "travelled": [stop_view(s) for s in travelled], "downstream": [stop_view(s) for s in downstream],
        "depots": depots, "terminusDepot": terminus,
        "homeDepot": depot_view(home, "home") if home else None,
        "helpline": LOST_FOUND_HELPLINE or None,
    })


@app.route('/api/routes')
def api_routes():
    """Return all routes with stop coordinates for the Leaflet map."""
    return jsonify(ROUTES)


# ── Transport catalog API (backs the passenger route/bus/seat selector) ───────
@app.route('/api/transport/routes')
def api_transport_routes():
    """
    Search the 500-route SETC catalog for the passenger/depot selector.
    Query params: q (free text over route_no / origin / destination), limit.
    """
    q = (request.args.get('q') or '').strip()
    try:
        limit = min(max(int(request.args.get('limit', 30)), 1), 100)
    except (TypeError, ValueError):
        limit = 30

    query = {}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query = {"$or": [
            {"route_no": rx}, {"origin_name": rx},
            {"dest_name": rx}, {"via": rx},
        ]}

    # depot_only=1 narrows the catalog to routes the logged-in depot may log
    # found items against, so the picker cannot offer a route that submit_found
    # would then reject. Mirrors depot_serves_route().
    if request.args.get('depot_only') == '1' and session.get('depot_phone'):
        depot = depots_collection.find_one({"phone": session['depot_phone']}) or {}
        scope = {"$or": [
            {"operating_depot_code": depot.get("depot_code")},
            {"stop_cities": (depot.get("city") or depot.get("stop") or "").strip().lower()},
        ]}
        query = {"$and": [query, scope]} if query else scope

    cursor = routes_collection.find(
        query,
        {"_id": 0, "route_id": 1, "route_no": 1, "origin_name": 1,
         "dest_name": 1, "via": 1, "type": 1, "distance_km": 1}
    ).sort("route_no", 1).limit(limit)
    return jsonify(list(cursor))


@app.route('/api/transport/routes/<route_id>')
def api_transport_route_detail(route_id):
    """Return a route with its ordered stops (names + coords) for the map."""
    route = get_route(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404
    stops = [
        {"stop_id": s["stop_id"], "name": s["name"],
         "lat": s.get("lat"), "lon": s.get("lon"), "is_major": s.get("is_major", False)}
        for s in route_stops(route)
    ]
    return jsonify({
        "route_id":    route["route_id"],
        "route_no":    route.get("route_no"),
        "origin_name": route.get("origin_name"),
        "dest_name":   route.get("dest_name"),
        "via":         route.get("via", ""),
        "type":        route.get("type"),
        "distance_km": route.get("distance_km"),
        "stops":       stops,
    })


@app.route('/api/transport/routes/<route_id>/geometry')
def api_transport_route_geometry(route_id):
    """
    Road-following geometry for a route as [[lat, lon], ...].

    Routes ORIGIN → DESTINATION via the public OSRM server and lets OSRM follow
    the real road corridor. The synthetic intermediate stops are deliberately
    NOT used as routing waypoints — some sit off-corridor and made OSRM produce
    paths that ended partway to the destination. The response is validated
    (both endpoints must snap onto roads near the true origin/destination) before
    it is accepted and cached on the route document. If OSRM is unavailable or
    the response fails validation, a straight line through the stops is returned
    and NOT cached, so a later view retries.
    """
    route = get_route(route_id)
    if not route:
        return jsonify({"error": "Route not found"}), 404

    # Serve cached geometry when available.
    if route.get("road_geometry"):
        return jsonify({"coords": route["road_geometry"],
                        "source": route.get("geometry_source", "osrm")})

    coord_stops = [s for s in route_stops(route)
                   if s.get("lat") is not None and s.get("lon") is not None]
    straight = [[s["lat"], s["lon"]] for s in coord_stops]
    if len(coord_stops) < 2:
        return jsonify({"coords": straight, "source": "straight"})

    origin, dest = coord_stops[0], coord_stops[-1]
    coords, source = straight, "straight"
    try:
        path = f"{origin['lon']},{origin['lat']};{dest['lon']},{dest['lat']}"
        resp = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{path}",
            params={"overview": "full", "geometries": "geojson"},
            timeout=6,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == "Ok" and data.get("routes"):
                geo = data["routes"][0]["geometry"]["coordinates"]   # [lon, lat]
                # Validate: every waypoint snapped onto a nearby road AND the
                # drawn path actually starts at the origin and ends at the dest.
                snaps_ok = all(w.get("distance", 0) <= 25000
                               for w in data.get("waypoints", []))
                ends_ok = bool(geo) and (
                    _haversine_km((geo[0][1], geo[0][0]), (origin["lat"], origin["lon"])) <= 25
                    and _haversine_km((geo[-1][1], geo[-1][0]), (dest["lat"], dest["lon"])) <= 25
                )
                if snaps_ok and ends_ok:
                    coords = [[lat, lon] for lon, lat in geo]
                    source = "osrm"
    except Exception as e:
        logger.warning(f"OSRM routing failed for route {route_id}: {e}")

    # Cache only a validated road route (failures retry on the next view).
    if source == "osrm":
        routes_collection.update_one(
            {"route_id": route_id},
            {"$set": {"road_geometry": coords, "geometry_source": "osrm"}}
        )
    return jsonify({"coords": coords, "source": source})


@app.route('/api/transport/routes/<route_id>/trips')
def api_transport_route_trips(route_id):
    """Return the trips (bus + departure timing) running on a route."""
    trips = list(trips_collection.find({"route_id": route_id}).sort("departure_time", 1))
    bus_ids = list({t["bus_id"] for t in trips})
    buses = {b["bus_id"]: b for b in buses_collection.find({"bus_id": {"$in": bus_ids}})}
    out = []
    for t in trips:
        bus = buses.get(t["bus_id"], {})
        out.append({
            "trip_id":         t["trip_id"],
            "bus_id":          t["bus_id"],
            "departure_time":  t.get("departure_time"),
            "arrival_time":    t.get("arrival_time"),
            "registration_no": bus.get("registration_no"),
            "type":            bus.get("type"),
            "capacity":        bus.get("capacity"),
        })
    return jsonify(out)


@app.route('/api/transport/depots/<phone>/routes')
def api_transport_depot_routes(phone):
    """Routes operated by a depot (with stop coords) — for the depot dashboard map."""
    depot = depots_collection.find_one({"phone": phone})
    if not depot:
        return jsonify([])
    routes = list(routes_collection.find(
        {"operating_depot_code": depot.get("depot_code")}
    ).limit(40))
    # Batch-fetch every stop referenced by these routes in one query.
    all_ids = {sid for r in routes for sid in r.get("stop_sequence", [])}
    stop_map = {s["stop_id"]: s for s in stops_collection.find({"stop_id": {"$in": list(all_ids)}})}
    out = []
    for r in routes:
        pts = [
            {"name": stop_map[sid]["name"], "lat": stop_map[sid].get("lat"),
             "lon": stop_map[sid].get("lon")}
            for sid in r.get("stop_sequence", []) if sid in stop_map
        ]
        out.append({
            "route_no":    r.get("route_no"),
            "origin_name": r.get("origin_name"),
            "dest_name":   r.get("dest_name"),
            "stops":       pts,
        })
    return jsonify(out)


@app.route('/api/transport/trips/<trip_id>/seatmap')
def api_transport_seatmap(trip_id):
    """Return the seat-map descriptor for the bus on a trip (visual picker)."""
    trip = get_trip(trip_id)
    if not trip:
        return jsonify({"error": "Trip not found"}), 404
    bus = get_bus(trip.get("bus_id"))
    if not bus:
        return jsonify({"error": "Bus not found"}), 404
    seatmap = build_seatmap(bus)
    seatmap["registration_no"] = bus.get("registration_no")
    seatmap["type"] = bus.get("type")
    return jsonify(seatmap)


# Groq retired llama3-8b-8192 (Aug 2025) and llama-3.1-8b-instant (Aug 2026);
# openai/gpt-oss-20b is its recommended replacement. Override with GROQ_MODEL.
GROQ_MODEL = (os.environ.get("GROQ_MODEL") or "openai/gpt-oss-20b").strip()
TRANSLATE_LANGS = {"ta": "Tamil", "hi": "Hindi"}


@app.route('/api/translate', methods=['POST'])
def api_translate():
    """
    Translate a spoken/typed description into English using Groq. The browser
    keeps the original text and shows both; on any failure it simply keeps the
    original, so a translation outage never loses what the passenger said.
    The model is set by GROQ_MODEL (see the note on GROQ_MODEL above).
    """
    data = request.get_json(silent=True) or {}
    text = data.get('text')
    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'No text provided'}), 400
    text = text.strip()[:1000]
    source = TRANSLATE_LANGS.get(data.get('lang'), "Tamil, Hindi, Tanglish or another Indian language")

    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        return jsonify({'error': 'Translation is not configured on this server.', 'code': 'not_configured'}), 503

    try:
        groq_client = groq.Groq(api_key=groq_api_key)
        chat_completion = groq_client.chat.completions.create(
            messages=[
                {
                    "role": "system",
                    "content": ("You are a translator for a bus lost-luggage service. The user's text is in "
                                f"{source}. Translate it into clear, concise English, keeping colours, brands, "
                                "names and item details exactly. Output only the English translation.")
                },
                {"role": "user", "content": text}
            ],
            model=GROQ_MODEL,
            temperature=0,
            # gpt-oss models reason before answering; keep that short and leave room for it.
            max_tokens=1200,
            **({"reasoning_effort": "low"} if GROQ_MODEL.startswith("openai/gpt-oss") else {}),
        )
        translated_text = chat_completion.choices[0].message.content.strip()
        return jsonify({'translated': translated_text, 'model': GROQ_MODEL})
    except Exception as e:
        logger.error(f"Translation error ({GROQ_MODEL}): {e}")
        return jsonify({'error': 'Translation failed. Your original text is kept.', 'code': 'failed'}), 502

# ── Counter security questions (anti-fraud gate before resolve) ─────────────
def _security_guard(kind):
    """(depot_phone, error_response) — staff session + per-depot and per-IP rate limits."""
    phone = session.get('depot_phone')
    if not phone:
        return None, (jsonify({"ok": False, "error": {"code": "not_authenticated",
                                                       "message": "Not authenticated"}}), 401)
    limit = SECURITY_LIMITS[kind]
    for ident in (f"depot:{phone}", f"ip:{request.remote_addr or 'unknown'}"):
        count, retry = security_limiter.recent_events(f"security-{kind}", ident, 600)
        if count >= limit:
            logger.warning("Security %s rate limit hit for %s.", kind, ident.split(':')[0])
            resp = jsonify({"ok": False, "error": {"code": "rate_limited",
                                                    "message": "Too many requests. Please wait a few minutes.",
                                                    "retryAfterSeconds": retry}})
            return None, (resp, 429)
    for ident in (f"depot:{phone}", f"ip:{request.remote_addr or 'unknown'}"):
        security_limiter.record_event(f"security-{kind}", ident, 600)
    return phone, None


def _security_call(kind, fn, *args):
    phone, error = _security_guard(kind)
    if error:
        return error
    try:
        return jsonify({"ok": True, **fn(*args, phone)})
    except HandoverError as e:
        return jsonify({"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}}), e.status
    except Exception:
        logger.exception("Security %s failed.", kind)
        return jsonify({"ok": False, "error": {"code": "server_error",
                                               "message": "Something went wrong. Try again."}}), 500


@app.route('/api/matches/<match_id>/security-questions', methods=['GET'])
def security_questions(match_id):
    """The check's state; questions only as text + options (never which one is right)."""
    return _security_call("questions", lambda mid, phone: handover.state(mid, phone), match_id)


@app.route('/api/matches/<match_id>/security-staff-check', methods=['POST'])
def security_staff_check(match_id):
    """Staff, holding the item, confirm photo-derived answers and may add questions."""
    body = request.get_json(silent=True) or {}
    picks = body.get("picks") if isinstance(body.get("picks"), dict) else {}
    added = body.get("added") if isinstance(body.get("added"), list) else []
    staff = body.get("staffName") if isinstance(body.get("staffName"), str) else ""
    return _security_call("staff-check", lambda mid, phone: handover.staff_check(mid, phone, picks, added, staff),
                          match_id)


@app.route('/api/matches/<match_id>/security-answers', methods=['POST'])
def security_answers(match_id):
    """The claimant's answers: returns only pass / escalate / fail / locked."""
    body = request.get_json(silent=True) or {}
    answers = body.get("answers") if isinstance(body.get("answers"), dict) else None
    if answers is None:
        return jsonify({"ok": False, "error": {"code": "invalid_answers", "message": "Answer every question."}}), 400
    return _security_call("answers", lambda mid, phone: handover.submit(mid, phone, answers), match_id)


@app.route('/api/matches/<match_id>/security-override', methods=['POST'])
def security_override(match_id):
    """After an escalation (or with too few questions): staff called / checked photo ID."""
    body = request.get_json(silent=True) or {}
    return _security_call("override", lambda mid, phone: handover.override(
        mid, phone, body.get("staffName"), body.get("idChecked"), body.get("idLast4")), match_id)


@app.route('/api/description/enhance', methods=['POST'])
def api_description_enhance():
    """
    Preview the cleaned description the matcher will use (logged-in passengers or
    depot staff only, so the API key cannot be used anonymously; 20 per 10 min).
    """
    if 'depot_phone' not in session:
        from passenger_auth.routes import authenticate_request
        try:
            authenticate_request()                     # a valid two-layer passenger session
        except Exception:
            return jsonify({"ok": False, "error": {"code": "not_authenticated", "message": "Log in first."}}), 401
    ident = request.remote_addr or 'unknown'
    count, retry = security_limiter.recent_events("describe", ident, 600)
    if count >= 20:
        return jsonify({"ok": False, "error": {"code": "rate_limited", "message": "Too many requests.",
                                               "retryAfterSeconds": retry}}), 429
    security_limiter.record_event("describe", ident, 600)
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str) or not 3 <= len(text.strip()) <= 2000:
        return jsonify({"ok": False, "error": {"code": "invalid_text", "message": "Send 3-2000 characters."}}), 400
    enhanced = description_enhancer.enhance(text)
    return jsonify({"ok": True, "enhanced": enhanced, "used": bool(enhanced)})


@app.route('/api/match/resolve', methods=['POST'])
def resolve_match():
    """
    Mark a specific match as resolved.
    Updates BOTH the matches collection AND the lost_reports collection.

    Expected JSON body:
      { "match_id": "<MongoDB ObjectId hex>" }

    Access control:
      The match must belong to the logged-in depot. The associated request_id
      is read from the match document (never trusted from the client), so a
      depot cannot resolve another depot's match or an arbitrary lost report.
    """
    if 'depot_phone' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data        = request.get_json(silent=True) or {}
    match_id    = data.get('match_id')
    depot_phone = session['depot_phone']
    depot_name  = session.get('depot_name', '')

    # Validate the match id (request_id is derived server-side from the match,
    # never trusted from the client).
    if not isinstance(match_id, str) or not match_id.strip():
        return jsonify({"success": False, "message": "match_id is required."}), 400
    match_id = match_id.strip()
    try:
        oid = ObjectId(match_id)
    except (InvalidId, TypeError):
        return jsonify({"success": False, "message": "Invalid match id."}), 400

    # Access control: a depot may only resolve matches that belong to it.
    match = matches_collection.find_one({"_id": oid, "depot_phone": depot_phone})
    if not match:
        return jsonify({"success": False, "message": "Match not found."}), 404
    if match.get("status") == "resolved":
        return jsonify({"success": False, "message": "Match already resolved."}), 409
    if match.get("status") not in MATCH_ACTIVE_STATUSES:
        return jsonify({"success": False,
                        "message": f"Match is {match.get('status')} and can no longer be resolved."}), 409
    item = found_collection.find_one({"found_id": match["found_id"]}) or {}
    if item.get("status") not in (None, "open"):
        return jsonify({"success": False,
                        "message": "This item is reserved by a passenger claim; use the claims desk."}), 409
    # Anti-fraud gate: nothing is handed over until the claimant passed the counter
    # questions (or staff verified them after an escalation). Enforced here, not
    # only in the page, so the check cannot be skipped by calling this route.
    if not resolve_allowed(match):
        logger.warning("Resolve refused for match %s: security check not passed (depot %s).",
                       match_id, mask_phone(depot_phone))
        return jsonify({"success": False, "code": "security_check_required",
                        "message": "Ask the claimant the security questions first."}), 409

    request_id = match["request_id"]   # trusted: comes from the match document

    # Step 1: Mark this match as resolved
    matches_collection.update_one(
        {"_id": oid},
        {"$set": {
            "status":      "resolved",
            "resolved_at": datetime.now().isoformat(),
            "resolved_by": depot_name
        }}
    )

    # Step 2: Mark the lost report as resolved
    # This prevents it from matching other found reports in the future.
    lost_collection.update_one(
        {"request_id": request_id},
        {"$set": {
            "status":        "resolved",
            "matched_depot": depot_name,
            "matched_at":    datetime.now().isoformat()
        }}
    )

    # Step 3: The item is now reserved for this report — end every competing match
    # (other reports on this item, other items on this report) and their queued
    # notifications.
    match_notifier.on_match_resolved(oid)
    # Step 4: Send WhatsApp notification to the passenger
    lost_report = lost_collection.find_one({"request_id": request_id})
    if lost_report and lost_report.get("phone"):
        msg = f"TNSTC Alert: Good news! Your lost item ({lost_report.get('tracking_id')}) has been verified and found at {depot_name}. Please visit the depot to collect it."
        notification_service.send_whatsapp_async(lost_report["phone"], msg)

    return jsonify({
        "success": True,
        "message": "Match resolved successfully."
    })


# Phase 2, Feature C: "Add new lost item" camera scan. OpenCV gates the frames;
# CLIP (image_sim.zero_shot), OCR and the offline captioner build the record,
# which is saved through the same create_found_report as the manual form.
intake_state = init_item_intake(app, db, create_found=create_found_report,
                                classifier=image_sim, ocr=ocr_ext)


@app.errorhandler(413)
def file_too_large(e):
    """Friendly response when an upload exceeds MAX_CONTENT_LENGTH (5 MB)."""
    flash('That file is too large. Please upload an image under 5 MB.', 'error')
    return redirect(request.referrer or url_for('index')), 413


if __name__ == '__main__':
    # Debug is OFF by default (the Werkzeug debugger is an RCE surface if exposed).
    # Enable locally with FLASK_DEBUG=1.
    debug = os.environ.get('FLASK_DEBUG') == '1'
    # Phone cameras (getUserMedia) only work on HTTPS or localhost. For testing
    # the depot scan from a phone on the same Wi-Fi, run with HTTPS_ADHOC=1
    # HOST=0.0.0.0 (self-signed certificate: the phone shows a one-time warning).
    ssl_context = 'adhoc' if os.environ.get('HTTPS_ADHOC') == '1' else None
    if os.environ.get('HOST', '127.0.0.1') in LOOPBACK:
        logger.warning("Listening on %s only: phones cannot open the QR codes. Start with `python start.py` "
                       "(or set HOST=0.0.0.0).", os.environ.get('HOST', '127.0.0.1'))
    app.run(debug=debug, use_reloader=False, host=os.environ.get('HOST', '127.0.0.1'),
            port=int(os.environ.get('PORT', '5003')), ssl_context=ssl_context)
