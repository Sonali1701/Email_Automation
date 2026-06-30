"""One-time festival/greeting sender (Independence Day) for clients or candidates.

A simple mail-merge: fills the first name into a fixed greeting template and
sends from your mailbox via Microsoft Graph. NO Claude, NO classification, NO
follow-up sequences — a one-time wish, separate from the outreach tool/DB.

Used by both the web UI (background sender with live progress) and this CLI.

Deliverability: each recipient gets an individual, personalized, plain-text
email (never a big BCC); sends are paced (--delay) so Microsoft 365 doesn't
treat the batch as a burst. See README for the domain-level checks (SPF/DKIM/
DMARC) that matter most for staying out of spam.

CLI examples:
  python greetings.py --audience candidate --excel "Candidate contact details.xlsx" --dry-run
  python greetings.py --audience candidate --excel "..." --test-email you@radixsol.com --limit 3
  python greetings.py --audience candidate --excel "..."
Re-runs are safe: real sends are recorded in greetings_sent.csv and skipped next
time, so a crash/throttle mid-batch won't double-send.
"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from graph_mailer import make_graph_mailer

load_dotenv()

# --- Templates (wording preserved from Independance_template) ----------------
CLIENT_BODY = """Hi {first_name},

On behalf of everyone at Radixsol, we wish you, your team, and your families a very Happy Independence Day.
As you celebrate this Fourth of July, we want to take a moment to express our sincere appreciation for your trust, partnership, and collaboration.

It has been a privilege to support your business, and we remain committed to delivering the talent, expertise, and service that contribute to your continued success.

We hope you enjoy a well-deserved break and a memorable holiday with family and friends.
Thank you for being a valued partner. We look forward to continuing our journey together and supporting your goals in the months ahead.
Wishing you a safe, relaxing, and joyful Independence Day.

Warm regards,
Radixsol Team"""

CANDIDATE_BODY = """Hi {first_name},

On behalf of everyone at Radixsol, we would like to wish you and your loved ones a very Happy Independence Day!
This Fourth of July, we want to take a moment to thank you for your dedication, hard work, and the value you bring every day.

Your commitment and professionalism are greatly appreciated, and we are proud to have you as part of the Radixsol family.
We hope you enjoy a well-deserved break, celebrate safely, and spend quality time with your family and friends.

Thank you for being an important part of our journey. We look forward to continuing our success together.
Wishing you a safe, joyful, and memorable Independence Day!

Warm regards,
Radixsol Team"""

GREETINGS = {
    "client": {"subject": "Happy Independence Day from Radixsol", "body": CLIENT_BODY},
    "candidate": {"subject": "Happy Independence Day from Team Radixsol", "body": CANDIDATE_BODY},
}

LEDGER = Path("greetings_sent.csv")
LOG = Path("greetings_log.csv")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email):
    return bool(_EMAIL_RE.match((email or "").strip()))


def first_name_of(name):
    parts = (name or "").strip().split()
    return parts[0] if parts else ""


def find_col(df, keywords):
    for col in df.columns:
        if any(k in str(col).strip().lower() for k in keywords):
            return col
    return None


def cellval(row, col):
    if col is None:
        return ""
    v = row.get(col, "")
    return "" if pd.isna(v) else str(v).strip()


def detect_columns(df):
    return find_col(df, ["name"]), find_col(df, ["email", "e-mail", "mail"])


def breakdown(df, name_col, email_col):
    """Quick data-quality summary for a greeting sheet."""
    seen, b = set(), {"sendable": 0, "no_email": 0, "invalid": 0, "duplicate": 0, "no_name": 0}
    for _, row in df.iterrows():
        name, email = cellval(row, name_col), cellval(row, email_col)
        e = email.lower()
        if not email:
            b["no_email"] += 1
        elif not valid_email(email):
            b["invalid"] += 1
        elif e in seen:
            b["duplicate"] += 1
        elif not first_name_of(name):
            b["no_name"] += 1
        else:
            b["sendable"] += 1
            seen.add(e)
    return b


def load_ledger(audience):
    """Emails already greeted for this audience (so re-runs don't double-send)."""
    sent = set()
    if LEDGER.exists():
        with LEDGER.open(encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("audience") == audience:
                    sent.add((r.get("email") or "").strip().lower())
    return sent


def append_ledger(audience, email, name):
    new = not LEDGER.exists()
    with LEDGER.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "audience", "email", "name"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), audience, email.lower(), name])


def _logrow(audience, name, email, recipient, status, detail):
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "audience": audience, "name": name, "email": email,
        "recipient": recipient, "status": status, "detail": detail,
    }


def write_log(rows):
    if not rows:
        return
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        if new:
            w.writeheader()
        w.writerows(rows)


def process(df, name_col, email_col, audience, mailer, *, dry_run=False,
            test_email=None, limit=None, delay=3.0, body_type="Text"):
    """Generator yielding progress events. Shared by the CLI and the web app.
    Real sends append to the ledger and skip anyone already greeted."""
    tpl = GREETINGS[audience]
    ledger = load_ledger(audience) if (not dry_run and not test_email) else set()
    rows = [(cellval(r, name_col), cellval(r, email_col)) for _, r in df.iterrows()]
    if limit:
        rows = rows[: int(limit)]
    total = len(rows)
    yield {"type": "start", "total": total, "audience": audience, "dry_run": dry_run}

    seen, log_rows = set(), []
    for i, (name, email) in enumerate(rows, start=1):
        first = first_name_of(name)
        e = email.lower()
        reason = None
        if not email:
            reason = "no email"
        elif not valid_email(email):
            reason = "invalid email"
        elif e in seen:
            reason = "duplicate in sheet"
        elif not first:
            reason = "no name"
        elif not dry_run and not test_email and e in ledger:
            reason = "already greeted"
        if reason:
            log_rows.append(_logrow(audience, name, email, "-", "skipped", reason))
            yield {"type": "skipped", "index": i, "total": total, "email": email, "reason": reason}
            continue
        seen.add(e)

        recipient = test_email or email
        if dry_run:
            log_rows.append(_logrow(audience, name, email, recipient, "preview", "dry-run"))
            yield {"type": "progress", "index": i, "total": total, "email": recipient,
                   "first_name": first, "status": "preview"}
            continue

        ok, detail = mailer.send(recipient, tpl["subject"], tpl["body"].format(first_name=first),
                                 body_type=body_type)
        if ok and not test_email:
            append_ledger(audience, email, name)
        log_rows.append(_logrow(audience, name, email, recipient, "sent" if ok else "error", detail))
        yield {"type": "progress", "index": i, "total": total, "email": recipient,
               "first_name": first, "status": "sent" if ok else "error", "detail": detail}
        if delay and i < total:
            time.sleep(delay)

    write_log(log_rows)
    yield {"type": "done", "total": total}


# --- CLI --------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Send Independence Day greetings")
    p.add_argument("--audience", required=True, choices=("client", "candidate"))
    p.add_argument("--excel", required=True, help="path to the contact spreadsheet")
    p.add_argument("--sheet", default=0, help="sheet name or index (default: first)")
    p.add_argument("--dry-run", action="store_true", help="preview only; send nothing")
    p.add_argument("--test-email", default=None, help="send every greeting to this address instead")
    p.add_argument("--limit", type=int, default=None, help="process at most N contacts")
    p.add_argument("--delay", type=float, default=3.0, help="seconds between sends")
    p.add_argument("--body-type", choices=("Text", "HTML"), default=os.getenv("BODY_TYPE", "Text"))
    return p.parse_args()


def main():
    args = parse_args()
    path = Path(args.excel)
    if not path.exists():
        sys.exit(f"Spreadsheet not found: {path}")
    df = pd.read_excel(path, sheet_name=args.sheet, dtype=str)
    name_col, email_col = detect_columns(df)
    if email_col is None:
        sys.exit(f"No email column found. Columns: {list(df.columns)}")
    print(f"Audience: {args.audience} | name column: {name_col!r} | email column: {email_col!r}")
    b = breakdown(df, name_col, email_col)
    print(f"Data: {b['sendable']} sendable, {b['duplicate']} duplicate, "
          f"{b['invalid']} invalid, {b['no_email']} no-email, {b['no_name']} no-name")

    mailer = None
    if not args.dry_run:
        mailer = make_graph_mailer()
        acct = mailer.get_account_silent()
        if not acct:
            sys.exit("Not signed in to Microsoft. Sign in via the web app first.")
        print(f"Sending as: {acct}")
        if args.test_email:
            print(f"TEST MODE: every greeting goes to {args.test_email}")
        if input(f"Send '{GREETINGS[args.audience]['subject']}'? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")

    sent = skipped = failed = 0
    for ev in process(df, name_col, email_col, args.audience, mailer, dry_run=args.dry_run,
                      test_email=args.test_email, limit=args.limit, delay=args.delay,
                      body_type=args.body_type):
        if ev["type"] == "progress":
            if ev["status"] == "sent":
                sent += 1
            elif ev["status"] == "error":
                failed += 1
            print(f"[{ev['index']}/{ev['total']}] {ev['email']} -> {ev['status']}")
        elif ev["type"] == "skipped":
            skipped += 1
            print(f"[{ev['index']}/{ev['total']}] {ev['email'] or '(no email)'} - skipped: {ev['reason']}")
        elif ev["type"] == "done":
            print(f"\nDone: {sent} sent, {skipped} skipped, {failed} failed. Log -> {LOG}")


if __name__ == "__main__":
    main()
