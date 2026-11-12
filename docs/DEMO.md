# Review demo — TNSTC Lost & Found

About 15 minutes. Two browser windows on the laptop: a normal window for the **passenger** and a
private window for **depot staff**, plus a phone on the same Wi-Fi to scan QR codes.

## 0. Start

```bash
python start.py --fresh
```

`--fresh` wipes every report, item, match and claim, so each run starts clean. Leave the window
open: OTPs appear there as `[MOCK SMS] ... purpose=otp: 123456`. It also prints the Wi-Fi address
phones use (e.g. `http://192.168.0.119:5003`).

| Who | Login |
|---|---|
| Passenger | Aadhaar **6123 4567 8904**, then PNR **T26100107** (Divya Prakash) |
| Depot staff | **Coimbatore Depot**, phone **9000000002**, password **pass123** |

Coimbatore is further along Divya's route (Chennai → Coimbatore, she got off at Cheyyar), so her bag
can be there. A depot the bus passed before she got off is ruled out by the luggage-path check.

## 1. Passenger: two-layer login and the report

1. **Passenger Login** → Aadhaar `6123 4567 8904`, tick the consent box → **Send OTP**.
   Type the OTP from the `start.py` window. Then PNR `T26100107`.
   - Layer 2 only accepts a ticket booked under the Aadhaar just verified (name match ≥ 90 %).
2. The dashboard shows the ticket's journey: route, where she boarded and got off, seat.
3. **Report a lost item**: route, bus, date and boarding stop are filled from the ticket. Write a
   description, e.g. *"Black Eastpak backpack with a steel tiffin box and a blue water bottle
   inside, red zip tag"*. The strength meter and its chips suggest what to add.
4. The success card shows an **encrypted QR**. Scan it with the phone: it opens the public
   verification page (status only, no personal details). A tampered or 72-hour-old code is refused.

## 2. Depot: log the found item

1. Staff window → **Depot Login** → Coimbatore Depot, `pass123`.
2. **Register Found Item (manual)**: the same route and date, a public description ("Black
   backpack"), private notes, a photo, the category **Bag**, and the ownership details only the
   owner would know: zip/tag colour **Red**, something inside **steel tiffin box**, brand
   **Eastpak**, a mark **name tag DIVYA**. These are stored only as hashes.
3. Matching runs at once. **Match Review** lists the match with its reasons (same trip, contents,
   brand, colour, text). Press **Approve**: the passenger is notified.

## 3. Passenger: prove ownership

1. Passenger window → **Notifications** → the match → **This is my item**.
2. Answer the questions (any mix of English, Tamil or Tanglish). It never says which answer was
   wrong; 3 failed rounds lock the claim for an in-person ID check.
3. Approved → the **pickup code and QR** appear.

## 4. Depot: pickup at the counter

1. Staff window → **Claims & Pickup**. Scan the passenger's QR with **Scan with camera**, or scan it
   with a phone: the QR is a link that opens this desk with the code filled in. Typing the code
   (e.g. `A7X2-K9P4`) also works. **Verify** → green **VALID**.
2. Item tag: on the dashboard, the item's **TAG-… · print tag** link shows the tag QR. Scan it or
   type the tag.
3. Divya logged in with Aadhaar, so ask for the **last 4 digits** (`8904`) and press **Send OTP**
   (OTP in the `start.py` window). **Hand over item** → **HANDED OVER**. The custody log shows it.

Alternative counter check: on the dashboard, **Resolve** on a match opens the security check
(staff confirm the details, then the claimant answers multiple-choice questions; 3 of 4 right
unlocks the handover).

## 5. Proof that it is tested

```bash
venv\Scripts\python -m pytest tests -q
```

[VERIFICATION.md](VERIFICATION.md) summarises the coverage; VERIFICATION_F1–F3 map every rule to
its test.
