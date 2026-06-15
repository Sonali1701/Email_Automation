"""Tracking store for the outreach + follow-up sequence.

Dual-backend: uses PostgreSQL when DATABASE_URL is set (cloud / Render),
otherwise a local SQLite file (desktop / dev). The SQL is written once with
%s placeholders and translated to ? for SQLite.

Tables are namespaced (outreach_*) so this can safely share an existing
Postgres database (e.g. your CRM's) without colliding with its tables.

One row per contact records where they are in the sequence so the follow-up
engine knows who to chase, when, and who has already replied/bounced. A small
kv table also holds the persisted Microsoft token cache so the hosted web app
and the daily trigger share one sign-in.

All timestamps are stored as UTC ISO-8601 strings with a trailing 'Z'
(e.g. 2026-06-11T13:40:00Z) so they sort lexicographically and compare directly
against Microsoft Graph's receivedDateTime values.
"""

import datetime as dt
import os
from pathlib import Path

DB_PATH = Path("outreach.db")
DATABASE_URL = os.getenv("DATABASE_URL")
IS_PG = bool(DATABASE_URL)

TBL = "outreach_contacts"
KV = "outreach_kv"

if IS_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TBL} (
  email          TEXT PRIMARY KEY,
  first_name     TEXT,
  last_name      TEXT,
  title          TEXT,
  company        TEXT,
  category       TEXT,
  step           INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'sent',
  subject        TEXT,
  first_sent_at  TEXT,
  last_sent_at   TEXT,
  next_due_at    TEXT,
  replied_at     TEXT,
  bounced_at     TEXT,
  last_error     TEXT,
  created_at     TEXT,
  updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_outreach_status_due ON {TBL}(status, next_due_at);
CREATE TABLE IF NOT EXISTS {KV} (k TEXT PRIMARY KEY, v TEXT);
"""


def get_intervals():
    """Days between consecutive emails (gap before each follow-up)."""
    raw = os.getenv("FOLLOWUP_INTERVALS", "2,5,7,10,20")
    out = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            try:
                out.append(int(x))
            except ValueError:
                pass
    return out or [2, 5, 7, 10, 20]


def now_utc():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_days(iso, days):
    base = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_next_due(step, last_sent, intervals):
    """When the next email is due given how many have been sent. None = done."""
    idx = step - 1
    if 0 <= idx < len(intervals):
        return _add_days(last_sent, intervals[idx])
    return None


# --- Backend plumbing ------------------------------------------------------
def connect():
    if IS_PG:
        con = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        cur = con.cursor()
        cur.execute(SCHEMA)
        con.commit()
        cur.close()
        return con
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def _ph(sql):
    return sql if IS_PG else sql.replace("%s", "?")


def _to_dict(row):
    if row is None:
        return None
    return dict(row) if IS_PG else {k: row[k] for k in row.keys()}


def _exec(con, sql, params=()):
    cur = con.cursor()
    cur.execute(_ph(sql), params)
    con.commit()
    cur.close()


def _all(con, sql, params=()):
    cur = con.cursor()
    cur.execute(_ph(sql), params)
    rows = cur.fetchall()
    cur.close()
    return [_to_dict(r) for r in rows]


def _one(con, sql, params=()):
    cur = con.cursor()
    cur.execute(_ph(sql), params)
    row = cur.fetchone()
    cur.close()
    return _to_dict(row)


# --- Sequence operations ---------------------------------------------------
def record_initial_send(con, *, email, first_name, last_name, title, company, category, subject, intervals):
    """Insert/restart a contact's sequence after the initial email goes out."""
    now = now_utc()
    nxt = compute_next_due(1, now, intervals)
    _exec(con, f"""
        INSERT INTO {TBL} (email, first_name, last_name, title, company, category,
            step, status, subject, first_sent_at, last_sent_at, next_due_at, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s, 1, 'sent', %s,%s,%s,%s,%s,%s)
        ON CONFLICT (email) DO UPDATE SET
            first_name=excluded.first_name, last_name=excluded.last_name, title=excluded.title,
            company=excluded.company, category=excluded.category, step=1, status='sent',
            subject=excluded.subject, first_sent_at=excluded.first_sent_at,
            last_sent_at=excluded.last_sent_at, next_due_at=excluded.next_due_at,
            replied_at=NULL, bounced_at=NULL, last_error=NULL, updated_at=excluded.updated_at
    """, (email.lower(), first_name, last_name, title, company, category,
          subject, now, now, nxt, now, now))


def get_active(con):
    return _all(con, f"SELECT * FROM {TBL} WHERE status='sent'")


def get_due_followups(con, now):
    return _all(con, f"SELECT * FROM {TBL} WHERE status='sent' AND next_due_at IS NOT NULL "
                     f"AND next_due_at <= %s ORDER BY next_due_at", (now,))


def mark_replied(con, email, when):
    _exec(con, f"UPDATE {TBL} SET status='replied', replied_at=%s, updated_at=%s WHERE email=%s",
          (when, now_utc(), email.lower()))


def mark_bounced(con, email):
    _exec(con, f"UPDATE {TBL} SET status='bounced', bounced_at=%s, updated_at=%s WHERE email=%s",
          (now_utc(), now_utc(), email.lower()))


def advance_after_followup(con, email, step, intervals):
    new_step = step + 1
    last = now_utc()
    nxt = compute_next_due(new_step, last, intervals)
    status = "completed" if nxt is None else "sent"
    _exec(con, f"UPDATE {TBL} SET step=%s, last_sent_at=%s, next_due_at=%s, status=%s, updated_at=%s "
               f"WHERE email=%s", (new_step, last, nxt, status, last, email.lower()))


def set_error(con, email, detail):
    _exec(con, f"UPDATE {TBL} SET last_error=%s, updated_at=%s WHERE email=%s",
          (str(detail)[:300], now_utc(), email.lower()))


def counts(con):
    out = {r["status"]: r["c"] for r in _all(con, f"SELECT status, COUNT(*) c FROM {TBL} GROUP BY status")}
    out["total"] = _one(con, f"SELECT COUNT(*) c FROM {TBL}")["c"]
    out["due_now"] = _one(con, f"SELECT COUNT(*) c FROM {TBL} WHERE status='sent' "
                               f"AND next_due_at IS NOT NULL AND next_due_at <= %s", (now_utc(),))["c"]
    return out


def report(con):
    """Campaign metrics for the dashboard. step counts emails sent to a contact
    (1 = initial), so (step - 1) is how many follow-ups they received."""
    row = _one(con, f"SELECT COUNT(*) c, COALESCE(SUM(step - 1), 0) fu FROM {TBL}")
    total = row["c"]
    followups_sent = int(row["fu"])
    by_status = {r["status"]: r["c"]
                 for r in _all(con, f"SELECT status, COUNT(*) c FROM {TBL} GROUP BY status")}
    due_now = _one(con, f"SELECT COUNT(*) c FROM {TBL} WHERE status='sent' "
                        f"AND next_due_at IS NOT NULL AND next_due_at <= %s", (now_utc(),))["c"]
    replied = by_status.get("replied", 0)
    return {
        "contacts": total,
        "initial_sent": total,
        "followups_sent": followups_sent,
        "total_emails": total + followups_sent,
        "replied": replied,
        "bounced": by_status.get("bounced", 0),
        "active": by_status.get("sent", 0),
        "completed": by_status.get("completed", 0),
        "errored": by_status.get("error", 0),
        "due_now": due_now,
        "reply_rate": round(100 * replied / total, 1) if total else 0.0,
    }


# --- Key/value (Microsoft token cache lives here in the cloud) --------------
def kv_get(con, key):
    row = _one(con, f"SELECT v FROM {KV} WHERE k = %s", (key,))
    return row["v"] if row else None


def kv_set(con, key, value):
    _exec(con, f"INSERT INTO {KV} (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
          (key, value))
