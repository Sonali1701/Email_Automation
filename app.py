"""Web frontend for the Radixsol email automation tool.

A thin Flask layer over the existing logic (templates.py, classifier.py,
graph_mailer.py). Serves a single-page UI and a small JSON/NDJSON API:

  GET  /                  -> the UI
  GET  /api/status        -> graph/claude/contacts readiness
  POST /api/signin/start  -> begin Microsoft device-code sign-in
  GET  /api/signin/poll   -> poll until sign-in completes
  POST /api/upload        -> upload a contacts .xlsx, returns parsed rows
  POST /api/preview       -> classify + render emails (no sending)
  POST /api/send          -> stream NDJSON progress while sending
  GET  /api/followups/status -> sequence counts (sent/replied/bounced/due)
  POST /api/followups/run    -> stream NDJSON while detecting replies + sending follow-ups

Run:  python app.py   then open http://127.0.0.1:5000
"""

import hmac
import json
import os
import threading
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import db
import followups
from classifier import classify_contact, make_anthropic_client
from graph_mailer import make_graph_mailer
from main import DEFAULT_MODEL, _log_row, _write_log, cell, first_name_of, load_signature, resolve_columns
from templates import render

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

# When APP_PASSWORD is set (cloud), protect every route with HTTP basic auth so a
# public URL can't send mail from the signed-in mailbox. Open in local dev.
APP_PASSWORD = os.getenv("APP_PASSWORD")


@app.before_request
def _require_auth():
    # The cron trigger guards itself with its own token (called by an external
    # scheduler that can't do the browser login), so skip basic auth for it.
    if request.path.startswith("/api/cron/"):
        return
    if not APP_PASSWORD:
        return
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.password or "", APP_PASSWORD):
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": 'Basic realm="Radixsol Email"'})


# Guard so overlapping cron pings don't start two runs at once.
_cron_running = threading.Event()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
CONTACTS_XLSX = UPLOAD_DIR / "contacts.xlsx"

# Singleton mailer + sign-in state shared across requests.
_mailer = None
_mailer_lock = threading.Lock()
_signin = {"status": "idle", "account": None, "error": None}
_signin_lock = threading.Lock()


def get_mailer():
    global _mailer
    with _mailer_lock:
        if _mailer is None:
            _mailer = make_graph_mailer()
        return _mailer


def read_contacts(limit=None):
    """Parse the uploaded spreadsheet into a list of contact dicts."""
    df = pd.read_excel(CONTACTS_XLSX, dtype=str)
    cols = resolve_columns(df)
    contacts = []
    for _, row in df.iterrows():
        email = cell(row, cols, "email")
        title = cell(row, cols, "title")
        company = cell(row, cols, "company")
        override = cell(row, cols, "category")
        last = cell(row, cols, "last_name")
        first = first_name_of(row, cols)
        if not any((email, title, company, override, last)):
            continue
        contacts.append({
            "first_name": first, "last_name": last, "email": email,
            "title": title, "company": company, "category_override": override,
        })
    if limit:
        contacts = contacts[: int(limit)]
    return {"columns": {k: str(v) for k, v in cols.items()}, "count": len(contacts), "contacts": contacts}


def build_email(contact, client, model, signature):
    """Classify + render one contact into {category, focus_area, subject, body}."""
    info = classify_contact(
        contact["title"], contact["company"], contact["first_name"],
        client=client, model=model, override=contact["category_override"],
    )
    subject, body = render(info["category"], info["focus_area"], info["opener"],
                           contact["first_name"], contact["company"], subject=info.get("subject"))
    full = f"{body}\n\n{signature}" if signature else body
    return {**contact, "category": info["category"], "focus_area": info["focus_area"],
            "subject": subject, "body": full}


# --- Static ----------------------------------------------------------------
@app.get("/")
def index():
    return send_file(Path(app.static_folder) / "index.html")


# --- Status ----------------------------------------------------------------
@app.get("/api/status")
def status():
    graph_configured = bool(os.getenv("GRAPH_CLIENT_ID"))
    account = None
    if graph_configured:
        try:
            account = get_mailer().get_account_silent()
        except Exception:
            account = None
    return jsonify({
        "graph_configured": graph_configured,
        "signed_in": bool(account),
        "account": account,
        "claude_enabled": bool(os.getenv("ANTHROPIC_API_KEY")),
        "has_contacts": CONTACTS_XLSX.exists(),
    })


# --- Sign-in (device code) -------------------------------------------------
def _complete_signin(flow):
    try:
        acct = get_mailer().complete_device_flow(flow)
        with _signin_lock:
            _signin.update(status="done", account=acct, error=None)
    except Exception as exc:
        with _signin_lock:
            _signin.update(status="error", error=str(exc))


@app.post("/api/signin/start")
def signin_start():
    if not os.getenv("GRAPH_CLIENT_ID"):
        return jsonify({"error": "GRAPH_CLIENT_ID is not set in .env"}), 400
    try:
        acct = get_mailer().get_account_silent()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    if acct:
        with _signin_lock:
            _signin.update(status="done", account=acct, error=None)
        return jsonify({"signed_in": True, "account": acct})

    try:
        flow = get_mailer().start_device_flow()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    with _signin_lock:
        _signin.update(status="pending", account=None, error=None)
    threading.Thread(target=_complete_signin, args=(flow,), daemon=True).start()
    return jsonify({
        "signed_in": False,
        "user_code": flow["user_code"],
        "verification_uri": flow.get("verification_uri") or flow.get("verification_url"),
        "expires_in": flow.get("expires_in"),
    })


@app.get("/api/signin/poll")
def signin_poll():
    with _signin_lock:
        return jsonify(dict(_signin))


# --- Upload ----------------------------------------------------------------
@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    if not f.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "Please upload an .xlsx file"}), 400
    f.save(CONTACTS_XLSX)
    try:
        return jsonify(read_contacts())
    except Exception as exc:
        return jsonify({"error": f"Could not read spreadsheet: {exc}"}), 400


# --- Preview ---------------------------------------------------------------
@app.post("/api/preview")
def preview():
    if not CONTACTS_XLSX.exists():
        return jsonify({"error": "Upload a contacts file first"}), 400
    data = request.get_json(silent=True) or {}
    limit = data.get("limit")
    use_claude = data.get("use_claude", True)

    info = read_contacts(limit=limit)
    client = make_anthropic_client() if use_claude else None
    model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
    signature = load_signature()

    out = [build_email(c, client, model, signature) for c in info["contacts"]]
    return jsonify({"count": len(out), "contacts": out, "claude_used": client is not None})


# --- Send (streaming NDJSON) ----------------------------------------------
@app.post("/api/send")
def send():
    if not CONTACTS_XLSX.exists():
        return jsonify({"error": "Upload a contacts file first"}), 400
    data = request.get_json(silent=True) or {}
    test_email = (data.get("test_email") or "").strip() or None
    dry_run = bool(data.get("dry_run"))
    limit = data.get("limit")
    delay = float(data.get("delay", 2))
    use_claude = data.get("use_claude", True)
    body_type = data.get("body_type", os.getenv("BODY_TYPE", "Text"))

    if not dry_run:
        try:
            acct = get_mailer().get_account_silent()
        except Exception as exc:
            return jsonify({"error": f"Auth error: {exc}"}), 400
        if not acct:
            return jsonify({"error": "Not signed in. Sign in to Microsoft first."}), 401

    info = read_contacts(limit=limit)
    client = make_anthropic_client() if use_claude else None
    model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
    signature = load_signature()

    def generate():
        total = len(info["contacts"])
        yield json.dumps({"type": "start", "total": total, "dry_run": dry_run}) + "\n"
        con = db.connect()
        intervals = db.get_intervals()
        log_rows = []
        sent = failed = 0
        try:
            for i, c in enumerate(info["contacts"], start=1):
                email = build_email(c, client, model, signature)
                recipient = test_email or c["email"]
                if dry_run:
                    status, detail = "preview", "dry-run"
                else:
                    ok, detail = get_mailer().send(recipient, email["subject"], email["body"], body_type=body_type)
                    status = "sent" if ok else "error"
                    # Start the follow-up sequence only for real sends to the real
                    # recipient (not test sends, which go elsewhere).
                    if ok and not test_email and c["email"]:
                        db.record_initial_send(
                            con, email=c["email"], first_name=c["first_name"], last_name=c["last_name"],
                            title=c["title"], company=c["company"], category=email["category"],
                            subject=email["subject"], intervals=intervals)
                if status == "sent":
                    sent += 1
                elif status == "error":
                    failed += 1
                log_rows.append(_log_row(c["first_name"], c["email"], c["company"], c["title"],
                                         email["category"], recipient, status, detail))
                yield json.dumps({
                    "type": "progress", "index": i, "total": total,
                    "first_name": c["first_name"], "email": c["email"], "recipient": recipient,
                    "category": email["category"], "subject": email["subject"],
                    "status": status, "detail": detail,
                }) + "\n"
                if not dry_run and delay and i < total:
                    time.sleep(delay)
        finally:
            con.close()
        if not dry_run:
            _write_log(log_rows)
        yield json.dumps({"type": "done", "sent": sent, "failed": failed, "total": total}) + "\n"

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


# --- Follow-ups ------------------------------------------------------------
@app.get("/api/followups/status")
def followups_status():
    con = db.connect()
    try:
        return jsonify({"report": db.report(con), "intervals": db.get_intervals()})
    finally:
        con.close()


@app.post("/api/followups/run")
def followups_run():
    data = request.get_json(silent=True) or {}
    dry_run = bool(data.get("dry_run"))
    limit = data.get("limit")
    delay = float(data.get("delay", 2))
    use_claude = data.get("use_claude", True)

    if not dry_run:
        try:
            acct = get_mailer().get_account_silent()
        except Exception as exc:
            return jsonify({"error": f"Auth error: {exc}"}), 400
        if not acct:
            return jsonify({"error": "Not signed in. Sign in to Microsoft first."}), 401

    client = make_anthropic_client() if use_claude else None
    signature = load_signature()
    intervals = db.get_intervals()

    def generate():
        con = db.connect()
        try:
            for ev in followups.process(con, get_mailer(), client, signature, intervals,
                                        dry_run=dry_run, limit=limit, delay=delay):
                yield json.dumps(ev) + "\n"
        finally:
            con.close()

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


def _run_cron_job():
    """Run the follow-up engine in the background (triggered by the cron URL)."""
    try:
        con = db.connect()
        try:
            for _ in followups.process(con, get_mailer(), make_anthropic_client(),
                                       load_signature(), db.get_intervals(),
                                       dry_run=False, delay=float(os.getenv("FOLLOWUP_DELAY", "2"))):
                pass
        finally:
            con.close()
    finally:
        _cron_running.clear()


@app.route("/api/cron/followups", methods=["GET", "POST"])
def cron_followups():
    """Daily trigger for an external free scheduler (cron-job.org, GitHub Actions).
    Call with ?token=<CRON_TOKEN>. Returns immediately; the run happens in the
    background so the caller's request timeout can't cut it off."""
    token = os.getenv("CRON_TOKEN")
    given = request.args.get("token") or request.headers.get("X-Cron-Token")
    if not token or not given or not hmac.compare_digest(given, token):
        return jsonify({"error": "invalid or missing token"}), 403
    if _cron_running.is_set():
        return jsonify({"status": "already running"}), 202
    try:
        if not get_mailer().get_account_silent():
            return jsonify({"error": "not signed in to Microsoft"}), 401
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    _cron_running.set()
    threading.Thread(target=_run_cron_job, daemon=True).start()
    return jsonify({"status": "started"}), 202


if __name__ == "__main__":
    print("Radixsol Email Automation UI  ->  http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
