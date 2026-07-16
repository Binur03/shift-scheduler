# Twilio WhatsApp Sandbox — Setup Checklist

Goal: a real shift-invite WhatsApp lands on **+1 720-584-4158** and the **Accept**
link in it is tappable.

---

## Part A — Twilio account + sandbox (in the browser)

- [ ] **1. Create a free Twilio account** at https://www.twilio.com/try-twilio
      (no credit card needed for the sandbox). Verify your email and your phone.
- [ ] **2. Open the WhatsApp sandbox:** Console → **Messaging → Try it out →
      Send a WhatsApp message**. You'll see:
      - a **sandbox number** (usually `+1 415 523 8886`)
      - a **join code** like `join <two-words>`
- [ ] **3. Join from YOUR phone (720-584-4158):** open WhatsApp and send the
      message `join <two-words>` to the sandbox number. You'll get a
      "You're all set" confirmation. *(The sandbox only delivers to numbers that
      have joined — so this step is required.)*
- [ ] **4. Copy your credentials** from the Console home page:
      - **Account SID** (starts with `AC…`, 34 chars)
      - **Auth Token** (click to reveal)

**→ Send me these three things and I'll fill in `.env`:**
1. Account SID
2. Auth Token
3. Sandbox number (e.g. `+14155238886`)

---

## Part B — Make the Accept link tappable (public URL)

The link in the message points at `PUBLIC_BASE_URL`. `localhost:8080` isn't
reachable from your phone, so expose the app with a tunnel:

- [ ] **5. Install ngrok:** https://ngrok.com/download (free account, one-time
      `ngrok config add-authtoken <token>`).
- [ ] **6. Run the tunnel:** `ngrok http 8080` → copy the `https://….ngrok-free.app`
      URL it prints.
- [ ] **7. Tell me that URL** (or set `PUBLIC_BASE_URL=` to it in `.env` yourself).
      *(On the free plan this URL changes each restart — update it each time.)*

---

## Part C — Run and test (terminal, in this folder)

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
flask --app app init-db                             # first time only
python app.py                                       # serves on :8080
```

Leave that running, and in a second terminal: `ngrok http 8080`.

- [ ] **8.** Go to `http://localhost:8080/admin`, log in (password = `ADMIN_PASSWORD`
      in `.env`, currently `change-me-locally`).
- [ ] **9.** Add yourself as a worker (Employees → `720-584-4158`), create a Job,
      create a Shift, then **Dispatch**.
- [ ] **10.** A WhatsApp arrives on your phone → tap the link → **Accept** →
      you should see the "confirmed" page.

---

## Notes
- Both the worker number and the admin-alert number must have joined the sandbox.
  Yours (+17205844158) is already set as `ADMIN_WHATSAPP_NUMBER`.
- Trial accounts can only message verified / sandbox-joined numbers — fine for you.
- For a permanent public URL (no ngrok), deploy to Cloud Run — see `README.md`.
