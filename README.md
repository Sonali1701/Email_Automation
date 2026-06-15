# Radixsol Email Automation

Reads a list of contacts from an Excel sheet, classifies each person by their job
title, picks the matching outreach template, personalizes it, and sends the email
from **your own Outlook/Microsoft 365 mailbox** via the Microsoft Graph API.

- **Excel in** → name, email, title, company (column names are auto-detected).
- **Claude classifies** each title into `healthcare` / `hr` / `procurement` /
  `executive` / `other`, writes a short personalized opener, and a tailored
  subject line. No API key? It falls back to keyword matching with fixed openers
  and subjects.
- **Your templates stay the skeleton.** Claude only does two bounded jobs — route
  to the right template and write the opener + subject. The body and all factual
  claims (Joint Commission / MBE-WBE, capability lists) are template-fixed and
  never AI-generated, and the opener is instructed to invent no facts.
- **Microsoft Graph sends** from your real mailbox; copies land in your Sent Items.
- Every run is logged to `send_log.csv`.

## 1. Install

Everything is already installed in your system Python. To set up a clean
environment elsewhere:

```powershell
pip install -r requirements.txt
```

## 2. Register an Azure app (one time, ~3 minutes)

This is what lets the tool send mail as you. No client secret is needed.

1. Go to <https://portal.azure.com> → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name it `Radixsol Email Automation`. For **Supported account types**, pick
   *Accounts in this organizational directory only* (single tenant) for a work
   account. Click **Register**.
3. On the **Overview** page, copy **Application (client) ID** and **Directory
   (tenant) ID**.
4. Go to **Authentication** → **Advanced settings** → set **Allow public client
   flows** to **Yes** → **Save**. (This enables device-code sign-in.)
5. Go to **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated permissions** → check **Mail.Send** and **User.Read** → **Add
   permissions**.
   - `Mail.Send` (delegated) does not require admin consent; you consent on first
     sign-in. If your org enforces admin consent, ask an admin to click
     **Grant admin consent**.

## 3. Configure

```powershell
copy .env.example .env
```

Edit `.env`:

- `GRAPH_CLIENT_ID` → the Application (client) ID from step 2.
- `GRAPH_TENANT_ID` → the Directory (tenant) ID (or leave as `common`).
- `ANTHROPIC_API_KEY` → optional; enables Claude classification + personalization.
- `ANTHROPIC_MODEL` → defaults to `claude-haiku-4-5-20251001` (cheap and fast).

Edit `signature.txt` with your real signature (it is appended to every email).

> Note: there is no free Claude API tier — `ANTHROPIC_API_KEY` needs credits.
> Leave it blank to run fully free with keyword-based classification.

## 4. Prepare your contacts

```powershell
python make_sample_contacts.py     # creates contacts.xlsx with example rows
```

Open `contacts.xlsx`, replace the examples with your real list. Columns:

| Column | Required | Notes |
|---|---|---|
| First Name | recommended | Used in the greeting. |
| Last Name | optional | Not currently used in the body. |
| Email | **required** | The Outlook/365 address to send to. |
| Title | recommended | Drives classification. |
| Company | optional | Used in subject + opener. |
| Category Override | optional | `healthcare` / `hr` / `procurement` / `other` to force a template and skip Claude. |

Header names are flexible — `Email`, `Email ID`, `Email Address`; `Title`,
`Job Title`, `Designation`; `Company`, `Company Name`, etc. all work.

## 5. Run — Web UI (recommended)

```powershell
python app.py
```

Open <http://127.0.0.1:5000>. The page lets you:

- **Sign in to Microsoft** in the browser (device code shown in a popup).
- **Drag in your `.xlsx`** and see the parsed contacts.
- **Preview emails** — each contact shows its classified category; click a row to
  read the full personalized email.
- **Send** with a test address, dry-run, limit, and delay — with a live progress
  bar and per-contact status.

The web UI reuses the same templates, classifier, and Graph sender as the CLI.

## 5b. Run — CLI

Always preview first:

```powershell
python main.py --dry-run                 # writes one .txt per contact to previews/
python main.py --dry-run --limit 3       # just the first three
```

Send a real test to yourself:

```powershell
python main.py --test-email you@radixsol.com
```

Send for real (first run opens a sign-in prompt — go to the URL shown, enter the
code, sign in once; the token is cached afterwards):

```powershell
python main.py
```

### Useful flags

| Flag | Effect |
|---|---|
| `--dry-run` | Build everything, send nothing; write previews to `previews/`. |
| `--limit N` | Process at most N contacts. |
| `--start N` | Skip the first N contacts (resume a batch). |
| `--test-email ADDR` | Send every email to ADDR instead of the real recipients. |
| `--no-claude` | Skip the API; classify by keywords only. |
| `--delay S` | Seconds between sends (default 3) to avoid throttling. |
| `--body-type HTML` | Send HTML instead of plain text. |
| `--excel PATH` | Use a different spreadsheet. |

## Follow-ups (automatic, reply-aware)

After the initial email, the tool chases non-repliers with a sequence of
follow-ups and **stops the moment someone replies or bounces**.

- **Cadence**: set by `FOLLOWUP_INTERVALS` in `.env` (default `2,5,7,10,20` —
  five follow-ups at +2, +5, +7, +10, +20 days from the previous email).
- **Reply detection**: reads your mailbox via Graph and checks whether each
  contact has emailed you since you reached out. Out-of-office auto-replies are
  ignored; bounces mark the address dead.
- **Content**: short `RE: <original subject>` bumps that escalate gently, each
  with one AI-personalized line (same no-invented-facts guardrails).
- **State**: tracked in `outreach.db` (SQLite). The initial send records each
  contact; the engine advances them through the sequence.

### One-time setup: add the `Mail.Read` permission

Reply detection needs to read your inbox, so add one permission:

1. Azure portal → your app registration → **API permissions** → **Add a
   permission** → **Microsoft Graph** → **Delegated** → check **Mail.Read** → add.
2. Re-run the app and **sign in again** (the cached token must pick up the new
   scope). If your org requires it, an admin clicks **Grant admin consent**.

Without `Mail.Read`, the engine **refuses to send follow-ups** (so it never
chases someone who may have already replied).

### Running follow-ups

From the web UI: the **Follow-ups** card shows counts and has **Preview due**
and **Run follow-ups now** buttons.

From the CLI:

```powershell
python followups.py --dry-run     # detect replies + show what's due, send nothing
python followups.py               # detect replies + send due follow-ups
```

### Make it run by itself (daily)

Register a Windows Task Scheduler job that runs the engine once a day:

```powershell
schtasks /Create /SC DAILY /ST 09:00 /TN "RadixsolFollowups" ^
  /TR "C:\Users\vs510\PycharmProjects\Email_Automation\run_followups.bat"
```

`run_followups.bat` runs `followups.py` and appends to `followups_run.log`.
Note: the task only fires when this PC is on at that time. For true 24/7
automation, host `followups.py` on an always-on machine (e.g. a daily cron on
your Render setup) using the same `.env` and `outreach.db`.

## Hosting it in the cloud (Render) — fully automatic

Running on Render makes follow-ups truly automatic (24/7, no PC needed). The app
is dual-backend: with `DATABASE_URL` set it uses **Postgres** + DB-stored token;
locally it stays on SQLite + a file. The included [render.yaml](render.yaml)
provisions three things: the **web service**, a **Postgres** database, and a
**daily cron** that runs the follow-up engine.

### Deploy

1. Push this repo to GitHub (your `.env`, `outreach.db`, `.msal_cache.bin`,
   `contacts.xlsx` are gitignored and stay local).
2. In Render → **New → Blueprint** → pick this repo. It reads `render.yaml` and
   creates the web service, the database, and the cron job.
3. Set the secret env vars (marked `sync: false`) in the Render dashboard for
   **both** the web service and the cron job:
   - `GRAPH_CLIENT_ID`, `GRAPH_TENANT_ID` — your Azure app (single-tenant is fine
     for a radixsol.com user).
   - `ANTHROPIC_API_KEY`
   - `APP_PASSWORD` — protects the web UI (you'll enter it in the browser).
4. Open the web service URL, enter `APP_PASSWORD`, and **sign in to Microsoft
   once** (device code). The token is saved in Postgres, so the daily cron reuses
   it silently — no re-login.
5. Make sure **`Mail.Read`** is granted on the Azure app (see the follow-up
   section) or reply detection won't run.

### How it runs once deployed

- You upload contacts and send from the web UI (as before).
- The **cron** runs `followups.py` every day at 09:00 UTC (14:30 IST — change the
  `schedule` in `render.yaml`), detects replies, and sends due follow-ups. No
  button, no PC.
- The **Reports** card shows live totals (emails sent, replied, due, etc.).

### Notes / limits

- This hosts **one sender** (whoever signs into that instance — all mail goes
  from their mailbox). Multiple people each sending from their own mailbox would
  be a separate multi-user build.
- Uploaded `.xlsx` files live on the web service's ephemeral disk, so re-upload
  after a redeploy before sending a new batch. The sequence data is safe in
  Postgres. Follow-ups need no file — they read from the database.
- Very large initial sends stream over one long HTTP request; if a batch exceeds
  the 600s server timeout, send it in chunks with the **Limit** field.

## How classification maps to templates

| Category | Template | Focus phrase example |
|---|---|---|
| `healthcare` | Clinical & locum staffing (the long one) | — |
| `hr` | Generic workforce | "Talent Acquisition & HR" |
| `procurement` | Generic workforce | "Procurement" |
| `executive` | Leadership / referral-oriented note | — |
| `other` | Generic workforce | Claude's best guess (e.g. "Supply Chain") |

`executive` covers senior leaders whose remit isn't specifically HR/procurement/
clinical (CEO, Managing Director, Country Head, GCC/site leader, CTO, business-unit
heads). They get a higher-altitude note asking for an intro to the right
staffing/procurement owner rather than a vendor-onboarding pitch. Procurement
keywords are tuned for India/manufacturing titles (Commercial, Purchase, Buyer,
Stores, Materials, Contracts).

Templates live in `templates.py` — edit the wording there. The personalized
opener (generic template only) is written by Claude and is instructed **not** to
invent facts about the company.

## Files

| File | Purpose |
|---|---|
| `main.py` | Orchestrator + CLI. |
| `templates.py` | Email bodies, subjects, rendering. |
| `classifier.py` | Claude classification + keyword fallback. |
| `graph_mailer.py` | Microsoft Graph auth (device flow) + sendMail. |
| `make_sample_contacts.py` | Generates `contacts.xlsx`. |
| `send_log.csv` | Append-only log of every processed contact. |

## Troubleshooting

- **`GRAPH_CLIENT_ID is not set`** — fill in `.env`.
- **`AADSTS65001 / consent`** — an admin must Grant admin consent for `Mail.Send`.
- **`AADSTS7000218` or device flow fails** — set *Allow public client flows = Yes*.
- **Wrong account signed in** — delete `.msal_cache.bin` and run again.
- **Claude errors** — the tool prints a warning and falls back to keywords; the
  run still completes.
