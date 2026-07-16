# Twilio WhatsApp Setup

Two modes:

- **Sandbox** (Part A–C): free, instant, for testing. Messages are free-form;
  only phones that have "joined" the sandbox receive them.
- **Production** (Part D): your own WhatsApp sender + Meta-approved message
  templates. Required for messaging real workers — **start Part D early, Meta
  approval can take days to weeks.**

---

## Part A — Twilio account + sandbox (in the browser)

- [ ] **1. Create a free Twilio account** at https://www.twilio.com/try-twilio
      (no credit card needed for the sandbox). Verify your email and your phone.
- [ ] **2. Open the WhatsApp sandbox:** Console → **Messaging → Try it out →
      Send a WhatsApp message**. You'll see:
      - a **sandbox number** (usually `+1 415 523 8886`)
      - a **join code** like `join <two-words>`
- [ ] **3. Join from YOUR phone:** open WhatsApp and send the message
      `join <two-words>` to the sandbox number. *(The sandbox only delivers to
      numbers that have joined.)*
- [ ] **4. Copy your credentials** from the Console home page into `.env`:
      `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_NUMBER`.

## Part B — Make the Accept link tappable (local testing)

The link in the message points at `PUBLIC_BASE_URL`. `localhost:8080` isn't
reachable from your phone, so expose the app with a tunnel:

- [ ] **5.** `ngrok http 8080` → set `PUBLIC_BASE_URL=` to the
      `https://….ngrok-free.app` URL it prints (changes each restart on the
      free plan).

## Part C — Run and test (terminal, in this folder)

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
flask --app app db upgrade                          # create/upgrade tables
python app.py                                       # serves on :8080
```

- [ ] **6.** Go to `http://localhost:8080/admin`, log in (`ADMIN_PASSWORD`).
- [ ] **7.** Add yourself as a worker, create a Job, create a Shift, **Dispatch**.
- [ ] **8.** A WhatsApp arrives → tap the link → **Accept** → "confirmed" page.

---

## Part D — Production: WhatsApp sender + approved templates

WhatsApp only allows a business to *initiate* a conversation using a
Meta-approved template. Free-form text (what the sandbox sends) is only
allowed as a *reply* within 24h of a worker messaging you. Since shift
invitations, reminders, and alerts are all business-initiated, production
needs all three as approved templates.

### D1. Register your WhatsApp sender

1. Console → **Messaging → Senders → WhatsApp senders → Create new sender**.
2. Follow the flow: connect your Meta Business account, verify the business,
   and register the phone number that will send messages (it must not be an
   active personal WhatsApp number).
3. When approved, put that number in `TWILIO_WHATSAPP_NUMBER`.

### D2. Create the three Content Templates

Console → **Messaging → Content Template Builder → Create new**. Category
**Utility** for all three. Language: English (adjust if your workers use
Spanish — you can submit both).

**Template 1 — Shift invitation** (name: `shift_invite`)
Type: *Call to action* (so it can carry the accept-link button).

Body:
```
New shift available: {{1}}
Location: {{2}}
Date: {{3}}
Time: {{4}} (~{{5}} hrs)

Tap below to respond — first come, first served.
```
Button: **Visit website**, dynamic URL:
`https://YOUR-CLOUD-RUN-DOMAIN/accept/{{6}}`
(The app fills `{{6}}` with the worker's private token. The domain is fixed
in the template, so create this template *after* you know your final URL —
your Cloud Run URL or custom domain.)

**Template 2 — Worker reminder** (name: `shift_reminder`)
Type: *Text*.

Body:
```
Reminder: you're confirmed for {{1}}
Location: {{2}}
{{3}} {{4}}

If you can no longer make it, contact your coordinator ASAP.
```

**Template 3 — Admin staffing alert** (name: `staffing_alert`)
Type: *Text*.

Body:
```
⚠️ Staffing alert: {{1}} at {{2}} on {{3}} ({{4}}) has {{5}} of {{6}} workers confirmed. Open the dashboard to adjust the schedule or re-dispatch.
```

Submit each for WhatsApp approval (button in the template editor). Status
shows in the Content Template Builder; approval is usually fast for Utility
templates but can take longer while your business is newly verified.

### D3. Wire the SIDs into the app

Each template has a **Content SID** (starts with `HX…`), shown in the
Content Template Builder. Set:

```
TWILIO_CONTENT_SID_INVITE=HX…    (shift_invite)
TWILIO_CONTENT_SID_REMINDER=HX…  (shift_reminder)
TWILIO_CONTENT_SID_ALERT=HX…     (staffing_alert)
```

When these are set the app sends via the approved templates; when blank it
falls back to free-form bodies (sandbox/dev only). The variable numbering
above matches what `utils/sms.py` sends — if you reword a template, keep the
`{{n}}` positions.

### Notes

- The **admin alert number** (`ADMIN_WHATSAPP_NUMBER`) receives templated
  messages too, so it works without joining anything.
- Twilio WhatsApp pricing is per-conversation; Utility conversations are
  cheap but not free — see https://www.twilio.com/whatsapp/pricing.
