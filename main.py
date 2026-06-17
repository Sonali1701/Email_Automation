"""Radixsol email automation.

Reads contacts from an Excel sheet, classifies each by job title (Claude, with a
keyword fallback), renders the matching outreach template, and sends each email
from your own mailbox via Microsoft Graph. Every send is logged to send_log.csv.

Examples:
  python main.py --dry-run                 # preview everything, send nothing
  python main.py --dry-run --limit 3       # preview the first 3
  python main.py --test-email you@x.com    # send all to yourself as a test
  python main.py                           # send for real
"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import db
from classifier import CATEGORIES, ClassifierError, classify_contact, make_anthropic_client
from graph_mailer import make_graph_mailer
from templates import render

load_dotenv()

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Spreadsheet header -> canonical field. Headers are matched case-insensitively.
COLUMN_ALIASES = {
    "first_name": ("first name", "firstname", "first", "fname", "given name", "name", "full name"),
    "last_name": ("last name", "lastname", "last", "surname", "family name"),
    "email": ("email", "email id", "email address", "e-mail", "mail", "outlook", "outlook email"),
    "title": ("title", "job title", "designation", "role", "position"),
    "company": ("company", "company name", "organization", "organisation", "org", "employer"),
    "category": ("category", "category override", "segment", "type"),
}


def resolve_columns(df):
    """Map the DataFrame's columns to canonical field names."""
    lookup = {}
    for field, aliases in COLUMN_ALIASES.items():
        for col in df.columns:
            if str(col).strip().lower() in aliases:
                lookup[field] = col
                break
    return lookup


def cell(row, cols, field):
    col = cols.get(field)
    if col is None:
        return ""
    val = row.get(col, "")
    if pd.isna(val):
        return ""
    return str(val).strip()


def first_name_of(row, cols):
    fn = cell(row, cols, "first_name")
    # If the matched column was a full name, keep only the first token.
    if fn and " " in fn and cols.get("first_name") and \
            str(cols["first_name"]).strip().lower() in ("name", "full name"):
        fn = fn.split()[0]
    return fn


def load_signature():
    sig_file = Path("signature.txt")
    if sig_file.exists():
        text = sig_file.read_text(encoding="utf-8").strip("\n")
        if text.strip():
            return text
    return os.getenv("SENDER_SIGNATURE", "").strip()


# --- Contact screening (don't email uncertain contacts to premium clients) ---
# Title values that mean "no real title".
JUNK_TITLES = {"", "not provided", "pending", "n/a", "n.a.", "na", "nil", "none",
               "null", "-", "--", "—", ".", "tbd", "to be decided", "unknown"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email):
    return bool(_EMAIL_RE.match((email or "").strip()))


def title_missing(title):
    return (title or "").strip().lower() in JUNK_TITLES


def has_valid_override(override):
    return str(override or "").strip().lower() in CATEGORIES


def prescreen(email, title, override, seen):
    """Reason to skip a contact BEFORE classifying (cheap checks), or None.
    `seen` is the set of lowercased emails already processed this run."""
    e = (email or "").strip().lower()
    if not e:
        return "no email address"
    if not valid_email(e):
        return "invalid email address"
    if e in seen:
        return "duplicate email"
    # A valid manual override is a deliberate choice, so a missing title is OK.
    if not has_valid_override(override) and title_missing(title):
        return "missing/unreadable title"
    return None


def skip_other(category, override):
    """Skip an auto-classified 'other' (uncertain) contact, unless the user
    deliberately set the category via override."""
    return category == "other" and not has_valid_override(override)


def parse_args():
    p = argparse.ArgumentParser(description="Radixsol email automation")
    p.add_argument("--excel", default="contacts.xlsx", help="path to the contacts spreadsheet")
    p.add_argument("--sheet", default=0, help="sheet name or index (default: first)")
    p.add_argument("--dry-run", action="store_true", help="preview only; send nothing")
    p.add_argument("--limit", type=int, default=None, help="process at most N contacts")
    p.add_argument("--start", type=int, default=0, help="skip the first N contacts")
    p.add_argument("--test-email", default=None, help="send every email to this address instead")
    p.add_argument("--no-claude", action="store_true", help="skip Claude; use keyword matching")
    p.add_argument("--delay", type=float, default=3.0, help="seconds to wait between sends")
    p.add_argument("--body-type", choices=("Text", "HTML"), default=os.getenv("BODY_TYPE", "Text"))
    return p.parse_args()


def main():
    args = parse_args()

    excel_path = Path(args.excel)
    if not excel_path.exists():
        sys.exit(f"Spreadsheet not found: {excel_path}\n"
                 f"Run 'python make_sample_contacts.py' to create a template.")

    try:
        df = pd.read_excel(excel_path, sheet_name=args.sheet, dtype=str)
    except Exception as exc:
        sys.exit(f"Could not read {excel_path}: {exc}")

    cols = resolve_columns(df)
    if "email" not in cols:
        sys.exit(f"No email column found. Columns present: {list(df.columns)}")
    if "title" not in cols:
        print("[warn] No title column found; everyone will fall back to keyword/other.")

    signature = load_signature()
    model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
    client = None if args.no_claude else make_anthropic_client()
    if client is None and not args.no_claude:
        sys.exit("Claude is enabled but ANTHROPIC_API_KEY is not set. "
                 "Set the key, or pass --no-claude to send deterministic (keyword) emails.")

    # Set up the mailer (and confirm the signed-in account) unless this is a dry run.
    mailer = None
    if not args.dry_run:
        mailer = make_graph_mailer()
        name, upn = mailer.whoami()
        print(f"\nSending as: {name} <{upn}>")
        if args.test_email:
            print(f"TEST MODE: every email will go to {args.test_email}")
        confirm = input("Proceed with sending? [y/N] ").strip().lower()
        if confirm != "y":
            sys.exit("Aborted.")

    # Tracking store for the follow-up sequence (only used for real sends).
    con = db.connect() if not args.dry_run else None
    intervals = db.get_intervals()

    # Select the slice of rows to process.
    rows = list(df.iterrows())[args.start:]
    if args.limit is not None:
        rows = rows[:args.limit]

    preview_dir = Path("previews")
    if args.dry_run:
        preview_dir.mkdir(exist_ok=True)

    if "title" not in cols:
        print("[warn] No title column detected — every contact will be skipped as "
              "'missing title'. Check your spreadsheet headers.")

    results = []
    seen = set()
    for n, (idx, row) in enumerate(rows, start=1):
        email = cell(row, cols, "email")
        title = cell(row, cols, "title")
        company = cell(row, cols, "company")
        override = cell(row, cols, "category")
        first_name = first_name_of(row, cols)

        # Silently skip fully-blank trailing rows.
        if not any((email, title, company, override, cell(row, cols, "last_name"))):
            continue

        # Cheap pre-checks: skip (and flag) before spending a Claude call.
        reason = prescreen(email, title, override, seen)
        if reason:
            print(f"[{n}] {email or '(no email)'} - SKIPPED: {reason}")
            results.append(_log_row(first_name, email, company, title, "-", email or "-", "skipped", reason))
            continue
        seen.add(email.strip().lower())

        try:
            info = classify_contact(title, company, first_name, client=client, model=model, override=override)
        except ClassifierError as exc:
            print(f"\nHALTED at row {idx}: {exc}\n"
                  f"Stopped to avoid sending non-AI emails. {len(results)} processed so far.")
            break

        # Skip uncertain 'other' classifications (unless deliberately overridden).
        if skip_other(info["category"], override):
            print(f"[{n}] {email} - SKIPPED: uncertain role ('other')")
            results.append(_log_row(first_name, email, company, title, "other", email, "skipped", "uncertain role (other)"))
            continue

        subject, body = render(info["category"], info["focus_area"], info["opener"], first_name, company, subject=info.get("subject"))
        full_body = f"{body}\n\n{signature}" if signature else body
        recipient = args.test_email or email

        label = f"[{n}] {first_name or '(no name)'} <{email}> | {title or '?'} -> {info['category']}"

        if args.dry_run:
            out = preview_dir / f"{n:03d}_{_safe(email)}.txt"
            out.write_text(
                f"To: {recipient}\nSubject: {subject}\nCategory: {info['category']} "
                f"(focus: {info['focus_area']})\n{'-' * 60}\n{full_body}\n",
                encoding="utf-8",
            )
            print(f"{label}  -> previews/{out.name}")
            results.append(_log_row(first_name, email, company, title, info["category"], recipient, "preview", out.name))
            continue

        ok, detail = mailer.send(recipient, subject, full_body, body_type=args.body_type)
        status = "sent" if ok else "error"
        print(f"{label}  -> {status}{'' if ok else ': ' + detail}")
        results.append(_log_row(first_name, email, company, title, info["category"], recipient, status, detail))

        # Start the follow-up sequence for real sends to the real recipient.
        if ok and not args.test_email and con is not None:
            db.record_initial_send(
                con, email=email, first_name=first_name, last_name=cell(row, cols, "last_name"),
                title=title, company=company, category=info["category"],
                subject=subject, intervals=intervals)

        if args.delay and n < len(rows):
            import time
            time.sleep(args.delay)

    _write_log(results)
    _summary(results, dry_run=args.dry_run)


def _log_row(first_name, email, company, title, category, recipient, status, detail):
    return {
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "first_name": first_name,
        "email": email,
        "company": company,
        "title": title,
        "category": category,
        "recipient": recipient,
        "status": status,
        "detail": detail,
    }


def _write_log(results):
    if not results:
        return
    path = Path("send_log.csv")
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        if new:
            writer.writeheader()
        writer.writerows(results)
    print(f"\nLog written to {path}")


def _summary(results, dry_run):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    verb = "previewed" if dry_run else "processed"
    parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"Done: {len(results)} {verb} ({parts or 'nothing'}).")


def _safe(s):
    return "".join(c if c.isalnum() or c in "._-@" else "_" for c in s)


if __name__ == "__main__":
    main()
