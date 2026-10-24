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

---

## Feature 2 — Automatic match notification

### F2-01 · Packaged like Feature 1; the matcher is hooked, not rewritten
All of Feature 2 lives in `match_notify/` and is wired in with `init_match_notify(app, db, ...)`.
The scorer (`score_lost_found`, `compute_and_save_matches`, `compute_matches_for_lost`) is
unchanged. Only `_save_match`, the persistence step both directions already call, now delegates to
`match_notifier.record_match()`. Its inline "≥ 0.80 → WhatsApp" alert is removed because it
bypassed dedupe, review and the privacy template. The staff re-run endpoint and
`manage_matches.py rerun` call the same two existing matching directions. Late-logged items
therefore still match older open reports, and new reports match older items.

### F2-02 · Lost reports are now owned (filing requires Feature 1 login)
"A passenger sees only their own reports" needs an owner, but the old form took only a typed name
and phone. `POST /lost` now requires `require_passenger` and stamps `ownerPnr` + `ownerPhone`
from the verified session. Name and phone come from the session, not the form. The anonymous
`/status` tracking page is unchanged. This completes F1-20.
**Legacy anonymous reports** (no owner) are still matched and shown to depot staff. Nobody can
verify them online, so their WhatsApp/email notifications are recorded as `failed` with
"report is not linked to a verified passenger". That makes them visible on the staff
notification table, and staff can phone the reporter as before.

### F2-03 · The existing `matches` collection is extended in place
The brief's `matches` fields (`reportId, itemId, confidence, status, createdAt, notifiedAt,
holdExpiresAt`) are added to the existing documents. Legacy fields (`request_id`, `found_id`,
`score`, `depot_phone`, `created_at`) are kept because the depot dashboard and resolve flow use
them. The new unique index on `(reportId, itemId)` is partial (`reportId` exists), so an
unmigrated legacy document can never block it. At startup an idempotent backfill gives legacy
documents the new fields and moves old `pending` matches to `pending_staff_review`. Nothing is
sent for migrated matches.

### F2-04 · Status model
| Status | Meaning |
|---|---|
| `pending_staff_review` | 0.60 ≤ confidence < 0.85. Invisible to the passenger, nothing sent. The query alias `?status=staff_review` is accepted. |
| `auto_approved` | Approved for notification, either automatically (≥ 0.85, `approvedBy: system`) or by staff (`approvedBy: staff:<depot>`). Delivery in progress. |
| `notified` | Delivery has settled. `notifiedAt` is set and the 7-day hold runs from it. |
| `claim_started` | Reserved for Feature 3. Never expires on its own. |
| `released` | Hold expired (`releaseReason: hold_expired`). The passenger sees **expired**. |
| `rejected` | `rejectedBy: system` (score < 0.60) or `staff:<depot>`. |
| `expired` | In the enum and displayed as "expired" like `released`. The job writes `released`, as the brief specifies. |
| `resolved` / `cancelled` | Pre-existing terminal states. `resolved`: the depot confirmed the match. `cancelled`: a competing match, the report or the item closed it. |

**Never resurrected.** Re-running an existing pair only updates `latestConfidence`. The one allowed
upward re-route is a *system* rejection (score was too low) that now scores into the review or
auto band, e.g. after the passenger adds detail to the report. Staff rejections, releases,
cancellations and resolutions are final. Both the `if` and the conditional update check
`rejectedBy: system`, and the test was checked to fail when both guards are removed.

### F2-05 · When does the hold start?
`notifiedAt` is set when the first external channel succeeds. If every channel fails, or the
passenger has none, it is set when the last channel gives up. The passenger can still see the
match in-app, and a hold that never started would reserve the item forever. So the hold is always
bounded at about 7 days after approval, while delivery failures stay on the staff table. Expiry is
exclusive (`now >= holdExpiresAt` means expired). The passenger view shows "expired" from that
instant, even before the release job runs.

### F2-06 · Delivery pipeline (the `notifications` collection is the queue)
On approval, the system first creates an `in_app` notification (`delivered` immediately) and a
**WhatsApp** job (`pending`). If WhatsApp fails permanently or exhausts its retries, an **email**
job is created. If the passenger has no WhatsApp or email on file, that channel's record is written
as `failed` with the reason, so staff see it. Transient errors retry with exponential backoff:
`NOTIFY_BACKOFF_BASE_SECONDS · 2^(attempt-1)` (30 s, 60 s, 120 s …, capped at
`NOTIFY_BACKOFF_MAX_SECONDS`), up to `NOTIFY_MAX_ATTEMPTS` (default 4, allowed 1–5). There is no
random jitter, so schedules are reproducible in tests; per-job `nextAttemptAt` already spreads
load. Permanent errors (4xx except 429, refused recipient, no contact) skip the remaining retries.

**Background job.** The matcher only inserts jobs and wakes the worker; the request path never calls
a provider. The worker is a daemon thread (`NOTIFY_WORKER_ENABLED`, every
`NOTIFY_WORKER_INTERVAL_SECONDS`) that also releases expired holds. A job is claimed by one atomic
update that sets `leaseUntil`/`leaseOwner`. So concurrent workers, several app processes, or
`manage_matches.py run-jobs` from cron never process the same job twice. A crashed worker's lease
expires after `NOTIFY_LEASE_SECONDS` and the job is picked up again.
**At-least-once caveat.** If a worker dies after the provider accepted a message but before writing
`sent`, one duplicate is possible after the lease expires. Neither WhatsApp nor SMTP offers
idempotency keys. The window is a single DB write, far shorter than the 120 s lease.

### F2-07 · Staff retry never double-sends
`POST /staff/notifications/:id/retry` works only on a `failed` WhatsApp/email job of a still-active
match. It also requires that no other external job of that match is `pending` or `sent`. Once the
passenger has been reached on any channel, or another attempt is in flight, retry returns `409`.
A retry resets that job's attempts and re-enters the normal pipeline.

### F2-08 · Contact details come from the booking
WhatsApp goes to the **verified** registered phone (the report's `ownerPhone`), provided the
passenger's booking row has `whatsapp: true`. A missing field counts as true; a non-WhatsApp number
then fails and falls back to email. Email comes from the booking's optional `email`. Contacts are
resolved at send time, so a corrected booking takes effect on retry. Only masked contacts are
stored on notification records.

### F2-09 · What a message may contain
Only these fields:
- the passenger's own name and report tracking ID;
- confidence %, the depot name and the depot's **public description**;
- the date found (dd-Mon-yyyy, an IST date);
- a login-protected link (`APP_BASE_URL/passenger/reports/<id>`).

Photos, private notes and other reports are never sent. Depot staff now enter a short **public
description** (required) separately from the **private notes** used for matching, because free-text
notes routinely contain contents and serial numbers. As defence in depth, the public description is
also redacted for emails, money amounts, long digit runs and serial-like tokens. Legacy items
without one show "Details withheld - please verify at the depot".
**Injection.** Untrusted text is inserted as a value, never evaluated as a template. It is
NFC-normalised, stripped of tags, control and bidi characters (ZWJ/ZWNJ are kept for Indic
scripts), truncated at a word boundary, and HTML-escaped (the email's HTML part, Jinja on pages).
The staff page builds DOM nodes with `textContent` only.

### F2-10 · One item, several reports
Each matching report is notified independently. Nothing in a message, view or API response reveals
that the item matched someone else. Feature 3 will enforce "only one claim may lock the item"; F2
leaves `found_reports.status` as `open` while several holds coexist. When a depot resolves one
match, `on_match_resolved` sets the item to `resolved` and cancels competing matches and their
queued notifications. New matches are never created for an item whose status is not `open`
(`claimed`, `handed_over`, `resolved`).

### F2-11 · Report lifecycle
- **Cancel:** a new `cancelled` report status. `POST /reports/:id/cancel` ends the report's active
  matches and cancels queued sends.
- **Update:** `PATCH /reports/:id {description}` updates an open report and re-runs the matcher for
  it (the "updated lost report" trigger).
- **Closed reports:** a cancelled or resolved report never gets new matches, and its queued jobs are
  cancelled at send time.
- **No early disclosure:** the anonymous status page shows "Match found" only after a match is
  approved, because approval is what moves the report to `matched`. The submit confirmation no
  longer reveals a count of unreviewed matches.

### F2-12 · Authorization
**Passengers.** Every lookup filters on `ownerPnr` + `ownerPhone` from the session. Anything not
owned returns **404**, indistinguishable from a missing id, so IDs cannot be probed. A passenger on
the same PNR with a different phone is a different owner. Matches become visible only after
approval (`approvedAt` exists).
**Staff.** Staff routes reuse the existing depot login (`session['depot_phone']`, verified against
`depots`); `require_staff` is the minimal role check and no admin role is added. Every staff query
is scoped to the depot's own items (`depot_phone`), and other depots' objects return 404. A staff
route returns 401 without a staff session, or **403** when the caller is a logged-in passenger.
Staff JSON POSTs keep CSRF protection and fail with `403 csrf_failed`.
**Known pre-existing weakness (out of scope).** Depot passwords are stored and compared in plaintext
(`depot_login`). Feature 2 uses that login as-is. Hashing them is recommended before production.

### F2-13 · Confidence thresholds vs the matcher's own threshold
The matcher still persists only pairs with `final ≥ 0.30` (`UnifiedScorer.MATCH_THRESHOLD`); that
is decided in its `compute_*` functions. Feature 2 then routes those pairs:
- ≥ `MATCH_AUTO_NOTIFY_MIN` (0.85): auto-notify;
- ≥ `MATCH_REVIEW_MIN` (0.60): staff review;
- otherwise: `rejected`, kept for audit and re-routing, hidden from the depot dashboard and passengers.

Both bounds are inclusive. Setting `MATCH_REVIEW_MIN` below 0.30 has no effect, because such pairs
never reach Feature 2.

### F2-14 · WhatsApp and email adapters
`WHATSAPP_PROVIDER=mock|twilio|meta`. `twilio` reuses the project's existing Twilio credentials
(`TWILIO_*`). `meta` is the WhatsApp Business Cloud API and sends free-form text, which Meta delivers
only inside the 24-hour customer-service window. Business-initiated production messages need a
pre-approved template registered in Meta Business Manager. `EMAIL_PROVIDER=smtp` sends UTF-8
text + HTML. All three real adapters were tested with fake transports only, not live accounts.
