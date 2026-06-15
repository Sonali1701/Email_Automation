"""Follow-up engine: detect replies, then send the next due follow-up to anyone
who hasn't replied or bounced.

Run manually:
  python followups.py --dry-run     # show what would happen, send nothing
  python followups.py               # detect replies + send due follow-ups
Or schedule it daily (see README) so it runs by itself.

Safety: on a real run, if reply detection fails (e.g. Mail.Read not yet
consented), the engine REFUSES to send follow-ups rather than chase people who
may have already replied.
"""

import argparse
import os
import time

from dotenv import load_dotenv

import db
from classifier import make_anthropic_client
from graph_mailer import GraphAuthError, make_graph_mailer
from main import load_signature
from templates import render_followup

load_dotenv()
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def process(con, mailer, client, signature, intervals, *, dry_run=False, limit=None, delay=2.0):
    """Generator yielding progress event dicts. See module docstring for safety."""
    body_type = os.getenv("BODY_TYPE", "Text")

    # --- 1. Detect replies and bounces among active sequences ---
    active = db.get_active(con)
    detected = {"replied": 0, "bounced": 0}
    detect_ok = True
    if active:
        since = min((c["first_sent_at"] for c in active if c["first_sent_at"]), default=db.now_utc())
        try:
            replies, _autos, bounced = mailer.scan_inbox_since([c["email"] for c in active], since)
        except Exception as exc:
            detect_ok = False
            yield {"type": "detect_error", "detail": str(exc)[:300]}
            if not dry_run:
                # Don't chase people blindly if we couldn't check for replies.
                yield {"type": "aborted", "reason": "reply detection failed; refusing to send"}
                return
            replies, bounced = {}, set()

        if detect_ok:
            for c in active:
                addr = c["email"].lower()
                if addr in bounced:
                    db.mark_bounced(con, addr)
                    detected["bounced"] += 1
                    yield {"type": "reply", "email": addr, "status": "bounced"}
                elif addr in replies and replies[addr] >= (c["first_sent_at"] or ""):
                    db.mark_replied(con, addr, replies[addr])
                    detected["replied"] += 1
                    yield {"type": "reply", "email": addr, "status": "replied"}
    yield {"type": "detect_done", **detected, "detect_ok": detect_ok}

    # --- 2. Send the next due follow-up to everyone still active ---
    due = db.get_due_followups(con, db.now_utc())
    if limit:
        due = due[: int(limit)]
    sent = failed = 0
    for i, c in enumerate(due, start=1):
        step = c["step"]                      # 1..5 -> follow-up number
        subject, body = render_followup(step, c["first_name"], base_subject=c["subject"])
        full = f"{body}\n\n{signature}" if signature else body

        if dry_run:
            yield {"type": "followup", "email": c["email"], "step": step,
                   "subject": subject, "status": "preview"}
            continue

        ok, detail = mailer.send(c["email"], subject, full, body_type=body_type)
        if ok:
            db.advance_after_followup(con, c["email"], step, intervals)
            sent += 1
            status = "sent"
        else:
            db.set_error(con, c["email"], detail)
            failed += 1
            status = "error"
        yield {"type": "followup", "email": c["email"], "step": step,
               "subject": subject, "status": status, "detail": detail}
        if delay and i < len(due):
            time.sleep(delay)

    yield {"type": "done", "sent": sent, "failed": failed, "due": len(due), **detected}


def main():
    ap = argparse.ArgumentParser(description="Radixsol follow-up engine")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; send nothing")
    ap.add_argument("--limit", type=int, default=None, help="send at most N follow-ups")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between sends")
    ap.add_argument("--no-claude", action="store_true", help="use fixed follow-up lines")
    args = ap.parse_args()

    con = db.connect()
    intervals = db.get_intervals()
    client = None if args.no_claude else make_anthropic_client()
    signature = load_signature()

    mailer = make_graph_mailer()
    try:
        acct = mailer.get_account_silent()
    except GraphAuthError as exc:
        print(f"Auth not configured: {exc}")
        return
    if not acct:
        print("Not signed in. Open the web app and sign in once (grant Mail.Read), then retry.")
        return
    print(f"Signed in as {acct}. Intervals (days): {intervals}\n")

    for ev in process(con, mailer, client, signature, intervals,
                      dry_run=args.dry_run, limit=args.limit, delay=args.delay):
        if ev["type"] == "reply":
            print(f"  {ev['email']}: {ev['status']}")
        elif ev["type"] == "detect_done":
            print(f"Detection: {ev['replied']} replied, {ev['bounced']} bounced "
                  f"({'ok' if ev['detect_ok'] else 'FAILED'})")
        elif ev["type"] == "detect_error":
            print(f"  [warn] reply detection failed: {ev['detail']}")
        elif ev["type"] == "aborted":
            print(f"ABORTED: {ev['reason']}")
        elif ev["type"] == "followup":
            tag = "would send" if args.dry_run else ev["status"]
            print(f"  FU#{ev['step']} -> {ev['email']}: {tag}")
        elif ev["type"] == "done":
            print(f"\nDone: {ev['sent']} follow-ups sent, {ev['failed']} failed, {ev['due']} were due.")


if __name__ == "__main__":
    main()
