"""Send mail through Microsoft Graph using delegated auth (device-code flow).

You sign in once interactively; the token is cached to disk so subsequent runs
are silent. Mail is sent from your own mailbox via POST /me/sendMail and a copy
is saved to your Sent Items.

Requires an Azure app registration with:
  - "Allow public client flows" = Yes (Authentication blade)
  - Delegated Microsoft Graph permissions: Mail.Send, User.Read
See README.md for the full walkthrough.
"""

import atexit
import html
import os
import re

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Send", "Mail.Read", "User.Read"]
DEFAULT_CACHE = ".msal_cache.bin"

# Subjects that signal an automatic reply (not a real human reply).
_AUTO = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|out[- ]of[- ]office|away from|on vacation|"
    r"on leave|abwesenheit|réponse automatique|respuesta automática)", re.IGNORECASE)
# Signatures of a bounce / non-delivery report.
_NDR = re.compile(
    r"undeliverable|delivery (has )?failed|delivery status notification|address not found|"
    r"recipient .*(reject|not found)|mailbox (is )?full|message blocked", re.IGNORECASE)


class GraphAuthError(RuntimeError):
    pass


class GraphMailer:
    def __init__(self, client_id, tenant_id="common", cache_path=DEFAULT_CACHE,
                 cache_load=None, cache_save=None):
        if not client_id:
            raise GraphAuthError(
                "GRAPH_CLIENT_ID is not set. Register an Azure app and add it to .env "
                "(see README.md)."
            )
        self.authority = f"https://login.microsoftonline.com/{tenant_id or 'common'}"

        # Token cache can be backed by a local file (desktop) or injected
        # load/save callables backed by the database (cloud).
        self._cache_path = cache_path
        self._cache_load = cache_load
        self._cache_save = cache_save
        self._cache = msal.SerializableTokenCache()
        blob = None
        if cache_load:
            try:
                blob = cache_load()
            except Exception:
                blob = None
        elif os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as fh:
                blob = fh.read()
        if blob:
            self._cache.deserialize(blob)
        atexit.register(self._save_cache)

        self.app = msal.PublicClientApplication(
            client_id, authority=self.authority, token_cache=self._cache
        )

    def _save_cache(self):
        if not self._cache.has_state_changed:
            return
        data = self._cache.serialize()
        if self._cache_save:
            try:
                self._cache_save(data)
            except Exception:
                pass
        else:
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                fh.write(data)

    def _token(self):
        result = None
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result:
            flow = self.app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise GraphAuthError(f"Failed to start device flow: {flow}")
            print("\n" + flow["message"] + "\n")  # tells you where to enter the code
            result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise GraphAuthError(
                f"Auth failed: {result.get('error')}: {result.get('error_description')}"
            )
        self._save_cache()
        return result["access_token"]

    # --- Web-friendly auth helpers (non-blocking device flow) ---------------
    def get_account_silent(self):
        """Return the signed-in username from cache without prompting, else None."""
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            return None
        self._save_cache()
        return accounts[0].get("username")

    def start_device_flow(self):
        """Begin device-code auth and return the flow dict (does not block)."""
        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise GraphAuthError(f"Failed to start device flow: {flow}")
        return flow

    def complete_device_flow(self, flow):
        """Block until the user finishes signing in; return the username."""
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise GraphAuthError(
                f"{result.get('error')}: {result.get('error_description')}"
            )
        self._save_cache()
        accounts = self.app.get_accounts()
        return accounts[0].get("username") if accounts else None

    def whoami(self):
        """Return (displayName, userPrincipalName) of the signed-in account."""
        token = self._token()
        r = requests.get(
            f"{GRAPH}/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"$select": "displayName,userPrincipalName,mail"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("displayName", "?"), data.get("userPrincipalName") or data.get("mail", "?")

    def send(self, to, subject, body, body_type="Text", cc=None, save_to_sent=True):
        """Send a single email. Returns (ok: bool, detail: str)."""
        token = self._token()
        if body_type.lower() == "html":
            content_type, content = "HTML", _to_html(body)
        else:
            content_type, content = "Text", body

        message = {
            "subject": subject,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]

        payload = {"message": message, "saveToSentItems": bool(save_to_sent)}
        r = requests.post(
            f"{GRAPH}/me/sendMail",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if r.status_code == 202:
            return True, "sent"
        return False, f"HTTP {r.status_code}: {r.text[:300]}"


    def scan_inbox_since(self, addresses, since_iso, max_pages=30):
        """Scan the mailbox for messages received since `since_iso` and classify
        them against our contact list. Returns (replies, autoreplies, bounced):

          replies     : {address -> earliest genuine reply timestamp}
          autoreplies : {address} that only sent automatic replies
          bounced     : {address} that produced a non-delivery report

        Requires the Mail.Read delegated permission.
        """
        token = self._token()
        addrset = {a.lower() for a in addresses}
        replies, autoreplies, bounced = {}, set(), set()
        url = f"{GRAPH}/me/messages"
        params = {
            "$select": "subject,from,bodyPreview,receivedDateTime",
            "$top": "50",
            "$orderby": "receivedDateTime desc",
            "$filter": f"receivedDateTime ge {since_iso}",
        }
        pages = 0
        while url and pages < max_pages:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                             params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            for m in data.get("value", []):
                frm = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
                subj = m.get("subject") or ""
                preview = m.get("bodyPreview") or ""
                recv = m.get("receivedDateTime") or ""
                blob = f"{subj} {preview}"
                if "postmaster" in frm or "mailer-daemon" in frm or _NDR.search(blob):
                    hay = blob.lower()
                    for a in addrset:
                        if a in hay:
                            bounced.add(a)
                    continue
                if not frm:
                    continue
                if _AUTO.match(subj):
                    autoreplies.add(frm)
                    continue
                if frm in addrset and (frm not in replies or recv < replies[frm]):
                    replies[frm] = recv
            url = data.get("@odata.nextLink")
            params = None  # nextLink already carries the query
            pages += 1
        return replies, autoreplies, bounced


def make_graph_mailer():
    """Build a GraphMailer using DB-backed token storage on Postgres (so the
    hosted web app and cron share one sign-in), or a local file otherwise."""
    import db  # local import avoids a hard dependency for non-mail uses

    kwargs = {}
    if db.IS_PG:
        def _load():
            con = db.connect()
            try:
                return db.kv_get(con, "msal_cache")
            finally:
                con.close()

        def _save(data):
            con = db.connect()
            try:
                db.kv_set(con, "msal_cache", data)
            finally:
                con.close()

        kwargs = {"cache_load": _load, "cache_save": _save}

    return GraphMailer(
        client_id=os.getenv("GRAPH_CLIENT_ID"),
        tenant_id=os.getenv("GRAPH_TENANT_ID", "common"),
        **kwargs,
    )


def _to_html(text):
    """Minimal text -> HTML conversion that preserves line breaks and bullets."""
    escaped = html.escape(text)
    return "<div style=\"font-family:Calibri,Arial,sans-serif;font-size:11pt\">" + \
        escaped.replace("\n", "<br>") + "</div>"
