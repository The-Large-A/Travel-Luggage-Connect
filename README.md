# AI-Assisted Passenger Belongings Recovery System
### 🚍 Intelligent Lost & Found Management for State Transport Services (TNSTC MVP)

The **AI-Assisted Passenger Belongings Recovery System** is a full-stack digital platform designed to automate and optimize the recovery of lost items in public transport. By leveraging **Sentence-BERT** for semantic matching and a custom **Luggage Path Logic** algorithm, the system bridge the gap between passengers and depot administrators, ensuring that lost items are tracked and returned with high efficiency.

---

## 📑 Project Abstract
In traditional transport systems, lost items are recorded in manual registers, leading to fragmented information and low recovery rates. This project introduces a centralized MongoDB-backed Flask application that:
1.  Allows passengers to report lost items with precise travel metadata.
2.  Provides depot staff with a secure dashboard to log found items (with image support).
3.  Automatically suggests matches using AI-driven similarity scores and route-based directional logic.

---

## ✅ Implementation Status (What's been implemented)

### **Backend & Logic**
- [x] **Flask Application Core**: Robust routing and session management.
- [x] **MongoDB Integration**: Persistent storage for lost reports, found reports, and depot credentials.
- [x] **Luggage Path Logic**: A directional algorithm that filters matches based on the bus's travel sequence (Stop A → Stop B → Stop C).
- [x] **AI Matching Engine**: Integrated `SentenceTransformer` (`all-MiniLM-L6-v2`) for semantic text similarity.
- [x] **Authentication**: Secure depot-specific login system.
- [x] **Passenger OTP Login (Feature 1)**: PNR + registered-mobile verification with a one-time password before Lost & Found access (see below).
- [x] **Automatic Match Notification (Feature 2)**: confidence-routed WhatsApp/email alerts with staff review, retries, fallback and a 7-day hold (see below).
- [x] **Secure Claims & QR Pickup (Feature 3)**: secret-question ownership check (English/Tamil/Tanglish), gated photo, item locking, single-use pickup code + encrypted QR, atomic handover with custody log (see below).

### **Frontend & UI**
- [x] **Passenger Portal**: Dynamic forms with real-time stop suggestions based on selected routes.
- [x] **Depot Dashboard**: Comprehensive view of submitted found reports and auto-suggested passenger matches.
- [x] **Responsive Design**: Modern Glassmorphism UI using CSS3 and Vanilla JS.
- [x] **File Uploads**: Image processing for found items using `werkzeug`.

### **Data & Setup**
- [x] **Database Seeding**: one script ([`seed.py`](seed.py)) builds the depots, the transport network from the official SETC timings, and the mock Aadhaar / PNR databases; `python start.py` runs it.
- [x] **Environment Configuration**: Secure `.env` handling for API keys and database URIs.

---

## 🛠️ Technical Stack

-   **Frontend**: HTML5, CSS3, JavaScript (ES6+), FontAwesome Icons.
-   **Backend**: Python 3.11+, Flask.
-   **Database**: MongoDB (NoSQL) via `pymongo`.
-   **AI/ML**: `sentence-transformers`, `scikit-learn` (Cosine Similarity).
-   **Security**: `python-dotenv`, `werkzeug.security`.

---

## 🧠 Core Algorithm: AI Matching Logic

### 1. Luggage Path Logic
Items lost on a bus typically stay on the bus until it reaches a depot or the end of its route.
-   **Example**: If a passenger boards at **Chennai** and gets down at **Villupuram**, the item is logicaly found at depots *after* Villupuram (e.g., **Salem**, **Erode**, or **Coimbatore**).
-   The system uses the `ROUTES` stop sequence to validate these "downstream" depots.

### 2. Evidence-based matching (`matching.py`, v2)
Every pair that passes the luggage-path filter is scored as **record linkage** (the
Fellegi–Sunter model used by Splink): each signal adds evidence in **bits**,
log2(P(signal | same item) / P(signal | different item)), and the total becomes a probability.

| Signal | Evidence |
|---|---|
| Journey | same trip (weaker when many items were found on it), same bus on a later run, seat proximity, days between journey and find; a **different bus** or an item logged before the journey counts against |
| Item | category (purse = wallet or handbag counts as compatible), colours (a rare red match weighs more than black; navy vs black is "close", red vs green is a contradiction), contents, material, the item's own brand (a different brand is a contradiction) |
| Identifiers | serial / IMEI, the passenger's phone number or full name on a tag (OCR) |
| Similarity | SBERT text, CLIP photo vs photo and **description vs photo**, each turned into a z-score against the other candidates, so a model that rates everything 0.5 adds nothing |

Missing data is neutral, correlated signals are capped, Tamil / Tanglish words are folded first
(MiniLM is English-only). **Auto-notify** needs P ≥ 0.85 **and** an identifying detail (brand,
serial, name, two contents, a near-identical photo, or a rare colour on the same item on the same
journey), no contradiction, a 2-bit lead over the runner-up, and that no other report or item
claims it as strongly. Staff see only the leading contenders, each with its reasons.

On the labelled benchmark (`python tools/match_benchmark.py`: 27 reports, 26 items, five black
bags on one trip, duplicates, wrong buses, Tamil reports) the v1 fixed-weight scorer sent **66
wrong auto-notifications** (same trip counted as proof); v2 sends **none**, surfaces every true
pair and ranks the true item first every time (R@1 1.0, v1 0.43).

---

## ▶️ How to Run the Project

One command sets up and starts everything (Windows, macOS or Linux):

```bash
python start.py
```

Needs only **Python 3.10+** (3.11 recommended) and an internet connection the first time.
`start.py` then:

1. creates `./venv` and installs `requirements.txt` (first run: several minutes);
2. finds MongoDB, or on Windows downloads the official MongoDB Community Server zip into
   `%USERPROFILE%\mongodb` (about 1 GB, SHA-256 checked). On macOS/Linux install MongoDB first
   (`brew install mongodb-community`, or your package manager);
3. starts MongoDB as a single-node replica set on `127.0.0.1:27017` (data in `~/mongodb/data`,
   outside the project);
4. seeds a fresh database with [`seed.py`](seed.py): depots, the transport network, the mock
   Aadhaar and PNR databases. There are no old reports or claims;
5. starts the app on `http://localhost:5003` **and** on the Wi-Fi address it prints, so phones on
   the same Wi-Fi can open the QR codes, and prints the review logins.

Ctrl+C stops the app and the database.

| Command | What it does |
|---|---|
| `python start.py` | set up whatever is missing, then start |
| `python start.py --fresh` | also delete every report, found item, match and claim first |
| `python start.py stop` | stop the database (if the app was closed some other way) |

**Review logins** (printed by `start.py` too):

| Who | Login |
|---|---|
| Passenger | Aadhaar **6123 4567 8904**, then PNR **T26100107** (Divya Prakash). The OTP is printed in the `start.py` window (`[MOCK SMS]`). |
| Depot staff | **Coimbatore Depot**, phone **9000000002**, password **pass123**. It is further along the passenger's route, so the item can be found there (a depot the bus passed before the passenger got off is ruled out by the luggage-path check). Others: Chennai 9000000001, Madurai 9000000003, Salem 9000000004, Tirunelveli 9000000005 |

Review-build settings (in `start.py`): no limits on OTP requests or on claims per day, OTPs printed
instead of sent, phones can log in over the Wi-Fi without HTTPS. `.env` is optional: copy
`.env.example` to `.env` only to add `GROQ_API_KEY` (Tamil voice translation, question wording) or
`OCR_API_KEY`. To send real SMS, set `SMS_PROVIDER=textbee` and `TEXTBEE_API_KEY` there.

If a phone cannot open the QR link, allow Python through the Windows firewall for **private
networks** (Windows asks the first time) and check the phone is on the same Wi-Fi.

### 6. Run the Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
The suite runs on an in-memory MongoDB (mongomock), so it needs no database or ML models.

What the suite covers beyond the unit tests:

| Command | What it checks |
|---|---|
| `python tools/match_benchmark.py` | v1 vs v2 matcher on 27 labelled reports / 26 items with hard negatives, replayed in arrival order (real SBERT; `--fake` for the test model) |
| `tests/test_matching.py` | every evidence rule, calibration, gates, odd input (emoji, injection, 20k chars, non-strings), 400 randomised pairs, order invariance |
| `tests/test_questions.py` | every category x question for sense, option contrast and flatness, and genuine owners vs four impostor types through the real hashing and scoring |
| `tests/test_handover.py` | name matching (Rajan ≠ Rajani), IP limits, encrypted QR (tamper, 72 h expiry, rate limit), question engine (contrast, exclusions, LLM validation), staff check, pass / escalate / lock, override, description meter and enhancer safety |
| `python tools/fetch_media.py` then `tests/test_media.py` | 70 freely licensed Commons photos and clips (18 of them candid everyday photos) (`datasets/media/ATTRIBUTION.md`) through real CLIP and OpenCV: same-object retrieval, description vs photo, zero-shot category and the question gate, colours, the camera-scan frame gate on video, and the matcher; skipped when the media is not downloaded |

---

## 🔐 Passenger Login: two security layers (Feature 1)

Someone who only knows another passenger's PNR must not be able to open their Lost & Found
records. Every login therefore passes **two layers, always in this order**. There is no
single-layer way in.

### Flow
1. **Layer 1 (Aadhaar / VID).** On **Passenger Login** (`/passenger/login`) the passenger enters
   a (mock) Aadhaar number or Virtual ID and consents. The number is checked (12 or 16 digits,
   Verhoeff) and looked up in the **encrypted mock UIDAI database**. A 6-digit OTP goes to the
   phone linked to that Aadhaar. The reply is the same generic message whether or not the number
   exists.
2. The passenger enters the OTP within **5 minutes** and gets a short-lived identity cookie. No
   Lost & Found data is reachable yet.
3. **Layer 2 (ticket PNR).** The passenger enters the ticket's PNR. It is looked up in the
   **encrypted mock PNR database** and accepted only if the booking carries the **same Aadhaar
   token** and an agreeing name. The booking must also be confirmed and travelled within 90 days.
   On a group booking the session is bound to that one passenger's seat.
4. On success a signed, `HttpOnly`/`Secure`/`SameSite=Lax` session cookie is issued (default
   30 min), and the passenger lands on **My Lost & Found** (`/passenger`). **Log out** revokes
   both layers.

**Protections.**
- OTPs come from a CSPRNG and are stored only as a keyed HMAC, compared in constant time and
  used once.
- **3 wrong attempts lock the number for 15 min**, and the right code is refused while locked.
- Resend cooldowns and per-number and per-IP limits apply.
- Login errors are generic, so neither layer reveals whether an Aadhaar or PNR exists.

### Both mock databases are encrypted at rest
`mock_uidai` and `bookings` store only a **blind index** (HMAC-SHA256: `aadhaarToken`/`vidToken`,
`pnrIndex`) next to an **AES-256-GCM** sealed record (`passenger_auth/vault.py`, `stores.py`):
- Names, phones, last 4 digits, routes and the PNR itself are never in clear.
- Each record is bound to its row through GCM associated data, so a ciphertext copied onto
  another row does not decrypt.
- MongoDB `$jsonSchema` validators reject any clear-text field.
- Clear-text rows left from older versions are sealed automatically at startup.

Keys: `DATA_ENCRYPTION_KEYS` (`k1:<base64 32 bytes>,k2:...`), `DATA_ENCRYPTION_KEY_ID` (the key that
encrypts new writes; older keys still decrypt, for rotation) and `DATA_INDEX_KEY`. If they are
unset, keys are derived from `SIGNING_SECRET` with a warning, which is fine for development only.

### API

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `POST /auth/id/otp/request` | `{"idNumber", "consent": "yes"}` | `202` generic message | `400`, `413`, `429` cooldown/limits |
| `POST /auth/id/otp/verify` | `{"idNumber", "otp"}` | `200` masked identity, sets identity cookie | `400`, `401` invalid/expired, `423`, `429` |
| `POST /auth/id/link` (`?next=/path` optional) | `{"pnr"}` | `200` passenger + session, sets cookie | `400`, `401` layer 1 missing, `404` PNR not under this Aadhaar |
| `GET /auth/id/me` | none | `200` the verified identity (masked) | `401` |
| `POST /auth/logout` | none | `200`, cookie cleared, session revoked | none |
| `GET /auth/me` | none | `200` passenger, booking, session expiry | `401` no/expired/invalid session, `403` booking no longer eligible |

POSTs need the CSRF token in an `X-CSRFToken` header, read from the `csrf-token` meta tag
like the existing pages. Errors have the shape
`{"ok": false, "error": {"code", "message", "retryAfterSeconds"?, "fields"?, "attemptsRemaining"?}}`.
Timestamps are UTC ISO-8601 (`...Z`) and are shown in IST in the UI.

### Protecting new routes (Features 2 & 3)
```python
from passenger_auth import require_passenger, current_passenger

@app.route("/api/my-claims")
@require_passenger                 # JSON 401/403 when not logged in
def my_claims():
    p = current_passenger()        # p.pnr, p.phone (E.164), p.passenger_name
    ...

@app.route("/my-claims")
@require_passenger(page=True)      # HTML pages: redirect to /passenger/login instead
def my_claims_page(): ...
```

### SMS provider
`SMS_PROVIDER=mock` (default) prints messages to the console and stores them in the `sms_log`
collection with the OTP masked. For local testing only, set `SMS_MOCK_EXPOSE_OTP=1` to see the
real code there. `SMS_PROVIDER=twilio` sends real SMS with `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN` and `TWILIO_SMS_FROM` (falls back to `TWILIO_PHONE_NUMBER`).

**Free real SMS for demos: `SMS_PROVIDER=textbee`.** [textbee](https://textbee.dev) sends SMS from an
Android phone's own SIM, so there is no per-message fee beyond your SMS pack and no DLT paperwork for a
small demo. Its hosted free tier has a monthly limit, and textbee is MIT-licensed if you prefer to
self-host. To set it up:
1. Install the textbee app on an Android phone with a SIM that can send SMS.
2. In the textbee dashboard, click *Add device*, scan the QR code with the app and copy the API key.
3. Set `SMS_PROVIDER=textbee` and `TEXTBEE_API_KEY=…` in `.env`. `TEXTBEE_DEVICE_ID` is optional.

This is meant for demos and testing. For production, use a DLT-registered Indian SMS provider.

### Sample PNRs (from `seed.py`)
| PNR | Mobile | Case |
|---|---|---|
| `T26100101` | 98765 43210 | confirmed, 2 days ago |
| `T26100102` | 94440 12345 | confirmed |
| `T26100103` | 98400 11223 | confirmed, Tamil passenger name |
| `T26100104` | 90033 12345 | **cancelled**: no OTP |
| `T26100105` | 91234 56780 / 80987 65432 | 3 passengers, two of them share a phone |
| `T26100106` | 70102 03040 | journey 120 days ago: outside the access window |

Design decisions are recorded in [`docs/DECISIONS.md`](docs/DECISIONS.md), and the
rule-to-test mapping is in [`docs/VERIFICATION_F1.md`](docs/VERIFICATION_F1.md).

---

## 🔔 Automatic Match Notification (Feature 2)

When the matcher pairs a lost report with a found item, passengers are notified automatically,
on WhatsApp first and then by email, through a privacy-safe message. Medium-confidence matches
wait for depot staff to approve them first.

### Flow
1. A passenger logs in (Feature 1) and files a lost report. The report is now **owned** by the
   verified PNR + phone, so only that passenger can see it.
2. The existing matcher runs in **both directions**: a new or updated report against logged
   items, and a newly logged item against older open reports.
3. Each pair is routed by the matcher's probability **and its gates** (see *Evidence-based matching*):

   | Confidence | Result |
   |---|---|
   | ≥ 0.85 (`MATCH_AUTO_NOTIFY_MIN`) and identifying, uncontradicted, clearly ahead | notified automatically |
   | ≥ 0.60 (`MATCH_REVIEW_MIN`), or ≥ 0.85 but generic / contested | **staff review** at `/staff/review`, with the reasons; nothing is sent before approval |
   | < 0.60, or not one of a block's leading contenders | rejected, never sent |

4. A background worker sends the message. If WhatsApp fails it retries with exponential backoff,
   then falls back to email. If every channel fails, staff see it in the delivery table and can
   retry. The passenger always gets an in-app notification.
5. The message links to `/passenger/reports/<TrackingID>`, which requires the Feature 1 login.
   The match page shows the depot, the depot's short public description, the date found (IST),
   the confidence and a **7-day hold** countdown, plus the "Start claim" button that Feature 3
   will enable.
6. After 7 days the hold is released and the passenger sees **expired**.

**What a message never contains:** photos, the depot's private notes, contents, serial numbers
or any hint of other passengers' reports. Depot staff now enter a short **public description**
(e.g. "Black backpack") separately from their **private notes**. The public text is also
sanitized and redacted automatically.

### API
Passenger (Feature 1 session; anything not yours is a 404):

| Method & path | Purpose |
|---|---|
| `GET /reports` | my lost reports |
| `PATCH /reports/<trackingId>` `{"description"}` | update an open report (re-runs matching) |
| `POST /reports/<trackingId>/cancel` | cancel an open report |
| `GET /reports/<trackingId>/matches` | privacy-safe matches for my report |
| `GET /matches/<matchId>` | one privacy-safe match (state, %, depot, description, date found, hold) |
| `GET /notifications` | my in-app notifications |

Staff (depot login; scoped to your depot; 401 when logged out, 403 for passengers):

| Method & path | Purpose |
|---|---|
| `GET /staff/matches?status=staff_review` | review queue (any match status accepted) |
| `POST /staff/matches/<id>/approve` · `/reject` | decide a review-band match (409 if already decided) |
| `GET /staff/notifications?status=failed` | delivery status, failures and errors |
| `POST /staff/notifications/<id>/retry` | retry a failed channel (409 if the passenger was already reached) |
| `POST /staff/matcher/rerun` `{}` or `{"itemId"}` | safely re-run the matcher for your open items |

Pages: `/passenger/reports/<id>`, `/passenger/matches/<id>`, `/passenger/notifications`,
`/staff/review`.

### Operations
```bash
python manage_matches.py run-jobs          # send due notifications once
python manage_matches.py release-expired   # release expired holds
python manage_matches.py rerun --report TRK-XXXXXXXX   # or --item <id> / --all-open
```
All of these are idempotent and safe while the app runs. Depot logins are listed under *How to Run*. Settings are listed in
`.env.example`. Set `MOCK_WHATSAPP_FAILURE=transient` to watch retries and the email fallback
locally.

Design decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md) (F2-01 … F2-14). Rule-to-test map:
[`docs/VERIFICATION_F2.md`](docs/VERIFICATION_F2.md).

---

## 🛡️ Secure Claims & QR Pickup (Feature 3)

A match notification alone does not let someone take an item. The passenger has to prove
ownership, and the depot hands the item over only against a signed, single-use pickup code.

### Flow
1. **Depot logs the item** with a category and a few **secret ownership details**, such as zipper
   tag colour, one thing inside, brand, a sticker/scratch, or the lock-screen wallpaper. They are
   hashed immediately and nobody can read them back. The item gets a printable **QR tag**
   (`TAG-XXXXXXXX`) and a custody-log entry.
2. **Passenger opens the match → "This is my item".** They answer 2–3 questions, chosen at random
   and varied by item category, and never answerable from the notification text.
   - Answers may be in **English, Tamil or Tanglish** ("yellow" / "manjal" / "மஞ்சள்"), and small
     typos are fine.
   - Only overall progress is shown, never which answer was wrong.
3. **Weighted trust score:**

   | Score | Result |
   |---|---|
   | ≥ 0.75 | approved automatically |
   | 0.50 – 0.75 | one stricter follow-up question, then staff review if still unsure |
   | < 0.50 | another round (max 3), then **locked** for an in-person photo-ID check |

4. **After verification** the found-item photo can be viewed through a 2-minute signed link. The
   item is locked to this claim (a second passenger can never also be approved). The passenger
   receives a **pickup code** (`A7X2-K9P4`) and a **QR**, valid 7 days, with download, print and
   countdown. The QR is **encrypted** (`TNSTC2`, AES-256-GCM with its own key): a photo of it reveals
   no claim, item tag, expiry or code, carries no PNR, and any edit is rejected. Old signed-only
   `TNSTC1` QRs are refused; the passenger reopens the page for a fresh one, or types the code.
5. **At the counter** staff scan the passenger's QR (or type the code) and the item's QR tag, check
   photo ID, and hand over. A single all-or-nothing step then:
   - writes the custody log (who, when, depot, staff, claim);
   - marks the item `handed_over`;
   - completes the claim and burns the code;
   - notifies the passenger.
6. **Lost phone?** After an ID check, staff can **reissue** the code, which revokes the old one.
   Cancelling or expiring a claim releases the item.

### API
Passenger (Feature 1 session; a claim is visible only to its owner, everything else is 404):

| Method & path | Purpose |
|---|---|
| `GET /claims[?matchId=]` | my claims |
| `POST /claims` `{"matchId"}` | "This is my item": open a claim (409 if one is open, the hold expired or the item is taken) |
| `GET /claims/<id>` | status and progress |
| `POST /claims/<id>/cancel` | cancel (releases the item) |
| `GET /claims/<id>/questions` | current questions: ids and text only |
| `POST /claims/<id>/answers` `{"answers": {id: text}}` | submit; returns status and progress only |
| `GET /claims/<id>/photo` | signed 120 s photo URL, only after verification (403 before) |
| `GET /claims/<id>/token` · `/token.svg` | pickup code + QR, only after approval (403 before) |

Staff (depot login, scoped to the depot holding the item):

| Method & path | Purpose |
|---|---|
| `GET /staff/claims?status=staff_review` | queue (also `locked`, `token_issued`, …) |
| `POST /staff/claims/<id>/approve` `{"idVerified"?}` | approve; `locked` claims require `idVerified: true` |
| `POST /staff/claims/<id>/reject` | reject |
| `POST /staff/claims/<id>/reissue-token` `{"idVerified": true}` | lost phone: new code, old one revoked |
| `POST /pickup/verify` `{"token", "qrTagId"?}` | read-only check with a reason (`expired`, `already_used`, `wrong_depot`, `tag_mismatch`, …) |
| `POST /pickup/complete` `{"token", "qrTagId", "idChecked": true, "staffName"?}` | atomic handover |

Pages: `/passenger/claims/<id>` (wizard, photo, token), `/staff/claims-desk` (scanner, queue,
reissue), `/staff/items/<foundId>/tag` (printable tag + custody log).

Feature 3 settings (`TRUST_THRESHOLD`, `TRUST_REVIEW_MIN`, `CLAIM_MAX_ROUNDS`,
`CLAIM_TOKEN_TTL_DAYS`, photo-link TTL, daily and IP limits) are listed in `.env.example`.
**`SIGNING_SECRET` must stay stable**, because changing it invalidates stored secret answers and
issued pickup codes.

Design decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md) (F3-01 … F3-14). Rule-to-test map:
[`docs/VERIFICATION_F3.md`](docs/VERIFICATION_F3.md). Summary of all three features:
[`docs/VERIFICATION.md`](docs/VERIFICATION.md). Live demo script: [`docs/DEMO.md`](docs/DEMO.md).

---

## 🧭 Phase 2 (Sem 7 final evaluation)

Branch `sem-7-final-eval`. Plan and research: the "Phase 2 Feature Plan" document.

| Feature | Where | What it does |
|---|---|---|
| **A. Two-layer login: Aadhaar, then PNR** | `passenger_auth/identity*.py`, `vault.py`, `stores.py`, `templates/passenger_login.html` | Always two layers: (mock) Aadhaar or VID + OTP to the linked phone, then the ticket PNR, whose booking must carry the **same ID token** and name. Both mock databases (UIDAI and PNR) are AES-256-GCM encrypted at rest with blind indexes. Reports keep only an HMAC token and the last 4 digits, purged 7 days after the case closes. |
| **B. Smart report form** | `index.html`, `static/js/report-form.js`, `photo-input.js`, `vocabulary.py` | Journey filled from the ticket (route, bus, date, boarding stop, seat taken server-side; alighting editable), category, where the item was kept, a match-readiness meter with add-detail chips (English/Tamil/Tanglish), photo dropzone with compression, Tamil voice shown next to its English translation. |
| **C. Camera scan for depots** | `item_intake/`, `templates/depot_intake.html`, `static/js/intake.js` | "Add new lost item": the webcam or phone camera opens; OpenCV keeps only sharp, well-lit, *new* views (Laplacian variance, exposure, perceptual hash), finds the item (GrabCut), names colours (k-means in Lab + CIEDE2000) and spots text for a close-up. CLIP zero-shot, OCR and offline Florence-2 write the record; it saves automatically. **Review build:** the dashboard button is hidden; the backend and `/depot/items/new` still work (next steps: `docs/NEXT_FEATURES.md` on the `Aarav` branch). |
| **D. Ownership questions v2** | `claims/attributes.py`, `claims/options.py`, `item_intake/questions.py` | Questions only for the kinds of item they suit, phrased with the item's noun ("the backpack's tag"); category-specific questions only when the camera is sure (≥ 0.8, 0.2 ahead). **Contrasting options**: colours an owner cannot mix up (no charcoal/navy/maroon pairs, ΔE00 ≥ 25), about as common as the true one; contents that fit the item. Hidden facts (inside, lock screen, tag name) weigh 2, visible ones ≤ 1; a round only verifies when a hidden fact was right; words the question suggests ("sticker") never count; a multiple-choice question is never repeated. Simulated (`tests/test_questions.py`): co-passenger impostors verified 3.4% (v1 19.8%), owners of items with a hidden fact 88% (v1 79%), no owner locked out. |
| **E. Luggage path, contacts, slip** | `map.js`, `/api/transport/routes/<id>/downstream`, `loss_slip.html` | Map: travelled part green, the bus's onward journey amber with downstream depot pins and public phone/WhatsApp; 72-hour "where is it now" card; printable loss slip with a QR to a public verify page. `/api/status` no longer exposes names or descriptions. |
| **F. Pickup re-check** | `claims/service.py`, `staff_claims.html` | Aadhaar claimants give the last 4 digits + an OTP sent to the Aadhaar-linked phone; the custody log keeps only the ID type and last 4. |

**Demo identities** (fictitious, Verhoeff-valid; full list in `passenger_auth/identity.py`):

| Passenger | Mock Aadhaar | PNR | Shows |
|---|---|---|---|
| Arun Kumar | 2345 6789 0124 | T26100101 | Aadhaar login → ticket autofill |
| Priya Raman | 3456 7890 1238 | T26100102 | email-only contact; Arun's Aadhaar + her PNR is refused at layer 2 |
| Meena Senthil | 7890 1234 5674 | T26100105 | group booking → bound to her own seat |
| Arun Kumar (VID) | 9123 4567 8901 2346 | T26100101 | same person via Virtual ID |

`python start.py` links the sample tickets to real trips. Phone cameras need HTTPS:
`HTTPS_ADHOC=1 HOST=0.0.0.0 python app.py`.
The description writer runs offline on the CPU (Florence-2: about 2 minutes to load the first
time, about a minute per item); set `INTAKE_VISION_PROVIDER=basic` for instant descriptions.

---

## 🔒 Final review: handover check, encrypted tracking QR, description meter

**Counter security check** (`handover_check.py`, `handover_service.py`, `static/js/handover_modal.js`).
A match can only be marked resolved after the claimant at the counter passes a multiple-choice
check, or staff complete an ID check after a failed or escalated check.

1. **When a match is created** the questions are built in the background (`HANDOVER_ASYNC=1`) and
   cached for 24 h. Sources: the found photos (OpenCV: GrabCut mask, HSV k-means colours named in
   Lab with CIEDE2000, edge density, shape, brightness, texture; CLIP zero-shot for material,
   pattern, style and features only when clearly sure), the depot's private notes and camera-scan
   attributes, and both descriptions. Facts the claimant already wrote in the lost report are
   never asked (they would prove nothing).
2. **Options contrast.** Wrong colours are never confusable with the true one or with any colour
   on the item (ΔE00 ≥ 25; black/navy, maroon/brown never paired), the most common wrong guess
   ("black", "leather", "plain") is included when it is wrong, and synonyms are never offered.
   Groq (`GROQ_REASONING_MODEL`) only rewords questions; every LLM output is validated (one
   question mark, no option text in the question, no "what object is this") or the template is used.
3. **Staff confirm first (step 1 of 2, screen kept from the claimant).** The camera's guesses are
   never asked unconfirmed: on candid photos it reads black wallets as brown or grey (see below).
   Staff pick the true answer, skip what they cannot tell, and can add questions (main / second
   colour, material, pattern, style) by choosing the true value; what they enter replaces the
   camera's guess. Agreement with the AI is logged.
4. **Claimant (step 2).** 4-6 questions × 4 options; the correct index never reaches the browser.
   ≥ 75 % right: pass, resolve unlocks. 50-74 %: escalate to a staff call / ID check. Below: fail,
   options reshuffled; 3 failed attempts lock the match (photo ID at the claims desk).
   Guessing 3 of 4 right at random: 5.1 %; 5 of 6: < 0.5 %.

**Tracking QR** (`tracking_qr.py`). Fernet-encrypted `{tracking ID, request ID, unix time}`, key
`QR_FERNET_KEY`. Issued on every lost report, saved as `static/uploads/qr/<request id>.svg`, shown
on the success card and the loss slip. Scans decrypt at `/verify/q/<token>`; tampered tokens and
tokens older than 72 h are refused, and only the claim status is returned.

**Description strength meter** (`static/js/description_meter.js`, `vocabulary.description_strength`).
Live on the passenger description and the depot notes: item type, colour, brand / unique mark,
contents, distinctive features → weak (0-30), moderate (31-60), strong (61-85), excellent (86-100),
with tappable chips for what is missing. `POST /api/description/enhance` cleans the text with Groq;
the result is stored separately (`descriptionEnhanced`, `notesEnhanced`) and used for SBERT
matching; it is rejected if it adds a colour, brand, content, material or number the original did not have.

**New and changed endpoints**

| Method | Path | Purpose | Auth | Rate limit |
|---|---|---|---|---|
| POST | `/auth/id/otp/request` | Layer 1: Aadhaar / VID → OTP | none (consent) | 5 / IP / hour |
| POST | `/auth/id/link` | Layer 2: PNR, same ID token, name ≥ 90 % | layer-1 session | 10 / IP / hour |
| GET | `/api/matches/<id>/security-questions` | Check state (staff view or claimant questions, no answers) | depot session, own match | 60 / 10 min per depot and IP |
| POST | `/api/matches/<id>/security-staff-check` | Staff confirm / skip / add questions | depot session | 30 / 10 min |
| POST | `/api/matches/<id>/security-answers` | Claimant's answers → pass / escalate / fail / locked | depot session | 20 / 10 min |
| POST | `/api/matches/<id>/security-override` | ID-checked handover after escalation | depot session, staff name | 10 / 10 min |
| POST | `/api/match/resolve` | Unchanged route; now 409 `security_check_required` until passed or overridden | depot session | — |
| GET | `/verify/q/<token>` | Public page for a scanned tracking QR | none | 30 / IP / 10 min |
| POST | `/api/qr/verify` | Decrypt a scanned QR → claim status only | none | 30 / IP / 10 min |
| POST | `/api/description/enhance` | Clean a description (stored separately) | depot or two-layer passenger session | 20 / 10 min |

**Measured on everyday photos** (`tests/test_media.py::test_candid_*`, 18 candid Commons photos:
wallets in hands and on cluttered tables, broken umbrellas on wet streets at night, a taped suitcase):
category right 18/18; main colour in the right family 14/17, but exactly right far less often
(black → grey/brown), so the camera's colour is never asked without staff confirming it; two
different black wallets never reach the "same photo" level (max CLIP 0.82 < 0.92). The synthetic
phone-condition stress tools are on the `Aarav` branch.

---

## 🏆 Impact & Roadmap
- **Social Impact**: Reduces passenger anxiety and increases trust in public transport.
- **Future Scope**:
    - "General ticket" path for passengers without a PNR (conductor ETM tickets: ticket number, route, time).
    - Optional cloud description writer (Claude Haiku) behind the existing provider switch.
    - Offline Secure QR verification of e-Aadhaar at pickup (UIDAI signature check).
    - Live GPS bus tracking and inter-depot transfers.
