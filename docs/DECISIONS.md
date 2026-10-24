# Design Decisions

Decisions taken while building the AI-Agent-Based Lost and Found Management System,
with the reasoning. Where the brief was ambiguous, the safest reasonable default was
chosen and recorded here.

---

## Feature 1 — OTP-based passenger authentication

### F1-01 · Packaged as a self-contained Flask blueprint
**Decision.** All of Feature 1 lives in `passenger_auth/` and is wired in with one call,
`init_passenger_auth(app, db)`, in `app.py`.
**Why.** `app.py` connects to MongoDB and loads ML models (SBERT/CLIP) at import time, so it
cannot be imported cheaply by tests. The blueprint can be mounted on a minimal Flask app
over mongomock, which makes the 179-test suite run in seconds. Features 2 and 3 import
`require_passenger` / `current_passenger` from the package.

### F1-02 · Session = signed cookie + server-side record (not a bare JWT)
**Decision.** On success the server stores a `passenger_sessions` record keyed by
SHA-256(session id) and sets cookie `tnstc_pax` holding an itsdangerous-signed
`{sid, pnr, ph, exp}`. Cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, `Max-Age=SESSION_TTL`.
**Why.** The project already uses signed cookies (Flask session), not JWTs. A pure
stateless token cannot satisfy "logout invalidation"; the server record can be revoked.
Storing only the hash of the sid means a database leak cannot be replayed as a cookie.
A session is valid only if the signature, the cookie `exp`, the server record, its
expiry/revocation and the `(pnr, phone)` binding all agree.
A dedicated cookie (instead of a key in Flask's `session`) keeps passenger auth separate from
depot staff login and signs it with its own `SIGNING_SECRET`.

### F1-03 · `SameSite=Lax`, not `Strict`
**Why.** Feature 2 sends match notifications that link into the portal. With `Strict`, a
logged-in passenger following that link would arrive without the cookie and be bounced to
login. Lax still blocks cross-site POSTs, and every state-changing auth POST also needs the
existing Flask-WTF CSRF token. Configurable through `PASSENGER_COOKIE_SAMESITE`.

### F1-04 · CSRF stays on for `/auth/*`; failures return 403 JSON
**Why.** The app already enforces CSRF on every POST. Login CSRF and logout CSRF are real
attacks, so the auth endpoints are not exempted. The front-end sends `X-CSRFToken` the same
way `depot.html` does. A CSRF failure on `/auth/*` returns `403 csrf_failed` in the standard
error shape instead of Flask-WTF's HTML 400 page.

### F1-05 · Booking data model (`bookings`)
**Decision.** One document per passenger:
`pnr, passengerSeq, passengerName, registeredPhone (E.164), status, route, travelDate (YYYY-MM-DD, IST date)`,
unique on `(pnr, passengerSeq)`. Field names follow the brief (camelCase). The existing
collections use snake_case; the brief's names were kept because Features 2/3 are specified
against them.
**Why.** "One PNR with multiple passengers or phones" means rows can share a PNR while each
passenger has their own registered phone.

### F1-06 · Multi-passenger PNRs
**Decision.** The typed phone is matched against every passenger on the PNR. The OTP goes to
that registered phone, and the session is bound to `(pnr, phone)`. If several passengers on the
PNR share that phone, `passengerName` is their names joined in passenger order
("Senthil Kumar, Meena Senthil"). A passenger with a different phone on the same PNR has an
independent OTP, attempt counter and lock.
**Why.** The phone is the possession factor being proven, so the identity is "whoever holds
this registered phone on this PNR".

### F1-07 · Cancelled and past-journey policy
**Decision.**
- `status` must be `confirmed` or `completed`. **Cancelled bookings never receive an OTP.**
- **Past journeys are allowed** as long as `travelDate` is within `BOOKING_ACCESS_WINDOW_DAYS`
  (default 90) of today in IST. Older journeys are refused. Future-dated bookings are allowed.
- Ineligible bookings get the **same generic 202** as an unknown PNR, so the response does not
  reveal that the PNR exists.
- Eligibility is re-checked on every protected request. If a booking is cancelled after login,
  the next request gets `403 booking_not_eligible` and the session is revoked.
**Why.** Items are almost always reported after the journey, so blocking past journeys would
defeat the feature. A cancelled ticket means the person never travelled. 90 days covers
realistic claim times without leaving old PNRs usable indefinitely.

### F1-08 · PNR format
`^[A-Z]{1,3}[0-9]{6,10}$` after trimming and upper-casing (e.g. `T26100101`). No official
TNSTC PNR specification was available. The pattern accepts the common
"letter prefix + digits" shape and rejects everything else (including Unicode look-alikes)
with `400`.

### F1-09 · Phone normalization
Accepts `+91`, `0091`, a bare `91` on 12-digit input, a leading trunk `0`, and spaces, dashes,
dots and parentheses. Everything normalizes to E.164 `+91XXXXXXXXXX`, and the result must be a
valid Indian mobile number (`[6-9]` + 9 digits). **Only ASCII digits are accepted.** Tamil or
full-width digits are rejected, not converted, so there are no look-alike surprises. Stored
`registeredPhone` values are normalized on comparison too, so legacy formats still match.

### F1-10 · Non-disclosure of PNR existence
`POST /auth/otp/request` returns the **same 202 body** for: valid booking, unknown PNR, phone
mismatch, cancelled booking and out-of-window booking. The OTP is sent **only** to the
registered number, never to the typed one.
The per-PNR hourly cap and the resend cooldown are applied **before** the booking lookup
outcome is used, and they apply identically to unknown PNRs, so 429s cannot be used as an
existence oracle either. SMS sending is mock-fast or asynchronous (Twilio on a background
thread), so timing does not reveal existence. The remaining timing difference is a few extra
DB writes.
**Accepted residual disclosure.** Once someone knows *both* the PNR and the correct registered
phone, they can observe `423 otp_locked` (lockout) and `otp_expired`. These states only exist for
a valid pair, and the brief requires the lockout message. The attacker still cannot get the OTP.

### F1-11 · OTP storage and comparison
6 digits from `secrets.randbelow` (CSPRNG). Stored only as
`HMAC-SHA256(K, salt ‖ pnr ‖ phone ‖ otp)`, where `K` is derived from `SIGNING_SECRET` with a
purpose label and `salt` is 16 random bytes per OTP. Compared with `hmac.compare_digest`.
Binding pnr and phone into the MAC means a code cannot be replayed against another booking.
The raw OTP is never logged, stored or returned. Logs carry masked phones only.

### F1-12 · Attempt counting is race-safe by construction
Each verify **reserves** an attempt with one conditional atomic update before it compares the
code: `$inc attempts WHERE status=active AND attempts<max AND not expired`. Then:
- wrong code with `attempts == max` → `active → locked` with `cooldownUntil = now + OTP_COOLDOWN_MINUTES`;
- correct code → `active → verified`, a conditional update only one request can win.

So N parallel guesses can never exceed `OTP_MAX_ATTEMPTS`, and N parallel correct submissions
produce exactly one session. "Max 3 wrong attempts" means three verification attempts per OTP.
A correct code on the 3rd attempt still succeeds.

### F1-13 · Lock semantics
The lock is per `(pnr, registered phone)` and outlives the OTP. During the cool-down:
- verify (even with the correct code) → `423`;
- **requesting a new OTP is also refused with `423`**. Otherwise a new OTP would reset the
  counter and make the lock pointless.

After the cool-down, a fresh OTP can be requested normally.

### F1-14 · Rate limits
| Limit | Scope | Mechanism |
|---|---|---|
| Resend cooldown 30 s | PNR + typed phone | atomic insert-or-conditional-update "gate" |
| 5 OTP requests / hour | PNR (any phone, even unknown PNRs) | sliding 1-hour event log, checked before the cooldown gate |
| 20 OTP requests / hour | client IP | fixed-window atomic `$inc` counter |
| 30 verify attempts / hour | client IP | fixed-window atomic `$inc` counter |

All limits are stored in MongoDB, so they hold across multiple app processes. **Trade-off:**
the per-PNR cap (required by the brief) lets someone who knows a PNR block OTP requests for it
for up to an hour. The client IP is `request.remote_addr`. **If deployed behind a reverse proxy,
configure `werkzeug.middleware.proxy_fix.ProxyFix`**, otherwise every user shares the proxy's
IP limit.

### F1-15 · HTTP status codes and error shape
Every error is `{"ok": false, "error": {"code", "message", ...}}`. `retryAfterSeconds` (plus a
`Retry-After` header) is added on 423/429, `fields` on validation errors and
`attemptsRemaining` on a wrong OTP.

| Code | When |
|---|---|
| 400 `invalid_request` | malformed/non-JSON body, non-object body, missing/unknown/non-string/too-long field, bad PNR/phone/OTP format |
| 401 `otp_invalid` / `otp_expired` | wrong, used, superseded or unknown OTP / expired OTP |
| 401 `session_missing` / `session_invalid` / `session_expired` | protected route without a valid session |
| 403 `booking_not_eligible` | valid session but booking cancelled/out of window since login |
| 403 `csrf_failed` | CSRF token missing or wrong |
| 413 `payload_too_large` | body over 2 KB. 413 is the precise status for this; the brief's list is not exhaustive |
| 423 `otp_locked` | lock cool-down active |
| 429 `rate_limited` / `pnr_request_limit` / `resend_cooldown` | the limits above |

Validation requires every field to be a JSON **string**, which rejects NoSQL operator
injection such as `{"$ne": null}`. Strings are then format-checked against strict ASCII
patterns, which rejects SQL-ish payloads, emoji and Unicode look-alikes.

### F1-16 · Time handling and clock skew
All timestamps are computed and stored in UTC (naive-UTC BSON dates, ms precision) and
returned as ISO-8601 with `Z`. They are displayed in IST (`Asia/Kolkata`, UTC+05:30) in the
templates. Journey dates are IST calendar dates. **The server clock is authoritative.** Expiry
bounds are exclusive (`now >= expiresAt` means expired). The API returns relative
`expiresInSeconds` / `retryAfterSeconds`, and the UI counts down with `performance.now()`, so
a wrong device clock cannot change what the user sees.

### F1-17 · Record retention
`otp_sessions`, `passenger_sessions`, `rate_limits`, `rate_limit_events` and `sms_log` carry a
`purgeAt` date with a TTL index (`expireAfterSeconds: 0`). The default retention is 24 h after
creation. A locked OTP record is kept until 24 h after its cool-down ends, so the lock is
always enforced before purge. Config validation refuses a retention shorter than the lock,
the session TTL or the 1-hour per-PNR window.

### F1-18 · SMS provider
`SMS_PROVIDER=mock` (default) logs to the console and the `sms_log` collection with the OTP
replaced by `******`. `SMS_MOCK_EXPOSE_OTP=1` writes the raw OTP instead; it is a development
flag, off by default, and logs a warning at startup. `SMS_PROVIDER=twilio` is a working
adapter on the existing `twilio` dependency, sent on a background thread (same reason as
commit 934a23f). Startup fails if its credentials are missing. It has been unit-tested with a
fake client but not against a live Twilio account. `send_message()` is available for
Feature 2 notifications.

### F1-19 · `SIGNING_SECRET` missing
If it is unset, a random per-process secret is generated with a loud warning, rather than
falling back to a guessable constant like the existing `SECRET_KEY` default. Sessions and
pending OTPs then do not survive a restart. A secret shorter than 32 characters is refused at
startup.

### F1-20 · Scope: existing anonymous flows unchanged
The existing anonymous report form (`/`) and tracking-ID status page (`/status`) are **not**
gated in Feature 1. Reports are not yet linked to PNRs, so there is nothing PNR-scoped to
protect. Gating them now would change working flows outside this feature. The protected
passenger home (`/passenger`) and `GET /auth/me` are the first `require_passenger` consumers.
Features 2/3 will attach PNR-linked reports and claims behind the same guard.

### F1-21 · Test tooling (dev-only dependencies)
`pytest` and `mongomock` are added in `requirements-dev.txt` only. mongomock is not
thread-safe, while real MongoDB makes each single operation atomic. The test fixture
serializes each collection call to reproduce exactly that guarantee, so the concurrency tests
are meaningful. The race tests hold all requests at barriers around the atomic attempt
reservation, which forces the worst-case interleaving deterministically. They were checked to
fail if either the single-use guard or the attempt cap is removed.
