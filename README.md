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

### **Frontend & UI**
- [x] **Passenger Portal**: Dynamic forms with real-time stop suggestions based on selected routes.
- [x] **Depot Dashboard**: Comprehensive view of submitted found reports and auto-suggested passenger matches.
- [x] **Responsive Design**: Modern Glassmorphism UI using CSS3 and Vanilla JS.
- [x] **File Uploads**: Image processing for found items using `werkzeug`.

### **Data & Setup**
- [x] **Database Seeding**: Python script (`seed_db.py`) to automatically populate the database with real-world routes (Chennai, Coimbatore, Madurai, etc.).
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

### 2. Semantic Description Matching
Traditional keyword search fails (e.g., "blue bag" vs "navy rucksack"). 
-   The system converts descriptions into **384-dimensional embeddings**.
-   **Cosine Similarity** compares the `Lost Description` vs `Depot Notes`.
-   **Threshold**: Set at `0.30` (the unified match score) to balance high sensitivity with relevance.

---

## ▶️ How to Run the Project

### 1. Prerequisites
- **Python 3.11+** installed.
- **MongoDB** instance (Local or Atlas) ready.

### 2. Setup Environment
Copy `.env.example` to `.env` and fill it in. At minimum:
```env
MONGO_URI=your_mongodb_connection_string
SECRET_KEY=your_random_secret_key
SIGNING_SECRET=at_least_32_random_characters   # passenger sessions + OTP hashing
```
Every passenger-auth setting (OTP lifetime, attempts, lock, rate limits, session TTL, SMS
provider) is listed with its default in `.env.example`.

### 3. Installation
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 4. Initialize Database
Before running the app, seed the depot authentication and route data, then the sample
passenger bookings (PNRs) used by passenger login:
```bash
python seed_transport.py
python seed_bookings.py
```

### 5. Start Application
```bash
python app.py
```
Visit `http://127.0.0.1:5003` in your browser.

### 6. Run the Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```
The suite runs on an in-memory MongoDB (mongomock), so it needs no database or ML models.

---

## 🔐 Passenger OTP Authentication (Feature 1)

Someone who only knows another passenger's PNR must not be able to open their Lost & Found
records. Passengers therefore prove they hold the **mobile number registered on the booking**.

### Flow
1. Passenger opens **Passenger Login** (`/passenger/login`) and enters the PNR and mobile number.
2. The server normalizes the number (`+91`, spaces, dashes and a leading `0` are all accepted)
   and checks it against the booking. If they match an eligible booking, a 6-digit OTP is sent
   **to the registered number only**. The response is the same generic message whether or not
   the PNR exists.
3. The passenger enters the OTP within **5 minutes**. Resend is available after **30 s**.
4. On success a signed, `HttpOnly`/`Secure`/`SameSite=Lax` session cookie is issued (default
   30 min), and the passenger lands on **My Lost & Found** (`/passenger`).
5. **Log out** revokes the session on the server. An expired session redirects to the login
   page with a notice.

**Protections.** OTPs come from a CSPRNG and are stored only as a salted HMAC, compared in
constant time and single use. A new request replaces the previous OTP. **3 wrong attempts
lock verification for 15 min**, and the correct code is refused while locked. Limits: one
request per 30 s, 5 per PNR per hour, plus per-IP limits. Cancelled bookings and journeys
older than 90 days cannot log in.

### API

| Method & path | Body | Success | Errors |
|---|---|---|---|
| `POST /auth/otp/request` | `{"pnr", "phone"}` | `202` generic message, `expiresInSeconds`, `resendAfterSeconds` | `400`, `413`, `423` locked, `429` cooldown/limits |
| `POST /auth/otp/verify` (`?next=/path` optional) | `{"pnr", "phone", "otp"}` | `200` passenger + session, sets cookie | `400`, `401` invalid/expired, `423`, `429` |
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

### Sample PNRs (from `seed_bookings.py`)
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

## 🏆 Impact & Roadmap
- **Social Impact**: Reduces passenger anxiety and increases trust in public transport.
- **Future Scope**:
    - Aadhaar-based identity verification for item collection.
    - Automated SMS/WhatsApp notifications via Twilio.
    - Computer Vision for automatic item categorization from photos.
    - Multi-language support (Tamil & English).
