"""Google Calendar for meeting recording - read-only, and straight between
Google and this computer (never through our server).

* :func:`connect`     one-time sign-in in the browser (OAuth for installed
                      apps: PKCE + a loopback redirect on a random local port).
                      Only the refresh token is kept, in the OS keyring.
* :func:`upcoming`    the next hours of meetings: title, attendees, call link.
* :func:`should_prompt` when to ask "record this meeting?".

Parsing and timing are pure functions, so they test without Google.
"""
import base64
import hashlib
import http.server
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

import requests

import storage

logger = logging.getLogger("transcribe.calendar")

# An OAuth client of type "Desktop app" (Google Cloud Console -> APIs & Services
# -> Credentials) in the project that has the Google Calendar API enabled. For
# installed apps Google does not treat this secret as confidential - it ships
# inside every copy of a desktop app. Config keys override both, for testing.
GOOGLE_CLIENT_ID = ""
GOOGLE_CLIENT_SECRET = ""
SCOPES = "openid email https://www.googleapis.com/auth/calendar.events.readonly"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
REFRESH_SECRET = "calendar_google_refresh_token"
LOGIN_TIMEOUT = 180
HTTP_TIMEOUT = 15
_SKIP_EVENT_TYPES = {"workingLocation", "outOfOffice", "focusTime"}

_OK_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>Transcribe</title>"
            "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f0f12;"
            "color:#fff;display:flex;height:100vh;align-items:center;justify-content:center;"
            "margin:0}.c{color:#22c55e;font-size:42px;text-align:center}</style></head>"
            "<body><div><div class='c'>&#10003;</div><h2>Calendar connected to Transcribe</h2>"
            "<p>You can close this tab and return to the app.</p></div></body></html>")
_FAIL_HTML = ("<!doctype html><html><head><meta charset='utf-8'><title>Transcribe</title></head>"
              "<body style='font-family:sans-serif'><h2>Calendar wasn't connected</h2>"
              "<p>Return to the app and try again.</p></body></html>")


class CalendarError(RuntimeError):
    """A user-readable reason the calendar couldn't be reached."""


class CalendarAuthError(CalendarError):
    """Access is gone (revoked, expired): the user must connect again."""


def _client(cfg=None):
    cfg = cfg or {}
    return ((cfg.get("google_oauth_client_id") or GOOGLE_CLIENT_ID).strip(),
            (cfg.get("google_oauth_client_secret") or GOOGLE_CLIENT_SECRET).strip())


def configured(cfg=None):
    """True when this build has a Google OAuth client to connect with."""
    return bool(_client(cfg)[0])


def is_connected():
    try:
        return bool(storage.read_secret(REFRESH_SECRET))
    except Exception:
        return False


# ── sign-in ──────────────────────────────────────────────────────────────────
def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class _Callback(http.server.BaseHTTPRequestHandler):
    """One-shot handler for Google's redirect back to 127.0.0.1."""

    # Per accepted connection: an idle socket (a browser preconnect, a port
    # scan) is dropped after this long instead of pinning the sign-in forever.
    timeout = 5

    def do_GET(self):  # noqa: N802 (http.server API)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)             # favicon and friends
            self.end_headers()
            return
        # Only Google's redirect carries our state. Anything else - a local
        # process or a web page probing ports with ?error=access_denied - is
        # refused and the real redirect is still waited for.
        expected = getattr(self.server, "expected_state", "") or ""
        got = query.get("state", [""])[0]
        if not expected or not secrets.compare_digest(got.encode(), expected.encode()):
            self.send_response(400)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in query.items()}
        body = _OK_HTML if "code" in query else _FAIL_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        return


def _email_from_id_token(id_token):
    """The account email from an OpenID id_token (display only - the token
    came straight from Google over TLS, so it isn't re-verified here)."""
    try:
        payload = (id_token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email") or ""
    except Exception:
        return ""


def connect(cfg=None, open_browser=webbrowser.open, timeout=LOGIN_TIMEOUT):
    """Sign in to Google Calendar in the browser; returns the account email.
    Blocking - run on a worker thread. Raises CalendarError."""
    client_id, client_secret = _client(cfg)
    if not client_id:
        raise CalendarError("Calendar connection isn't set up in this version yet.")
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(24)
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    except OSError as e:
        raise CalendarError(f"Couldn't start the sign-in listener: {e}")
    server.timeout = 1
    server.result = None
    server.expected_state = state
    redirect = f"http://127.0.0.1:{server.server_address[1]}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent",
    })
    try:
        open_browser(url)
        deadline = time.time() + timeout
        while server.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    res = server.result or {}
    if not res:
        raise CalendarError("Sign-in timed out - try again.")
    if res.get("state") != state:
        raise CalendarError("Sign-in failed a security check - try again.")
    if res.get("error"):
        raise CalendarError("Google sign-in was cancelled." if res["error"] == "access_denied"
                            else f"Google sign-in failed ({res['error']}).")
    data = {"code": res.get("code", ""), "client_id": client_id, "redirect_uri": redirect,
            "grant_type": "authorization_code", "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise CalendarError(f"Couldn't reach Google: {e}")
    if r.status_code != 200:
        raise CalendarError(f"Google refused the sign-in (HTTP {r.status_code}).")
    tok = r.json()
    if "calendar" not in (tok.get("scope") or ""):
        # Google shows each permission as its own checkbox.
        raise CalendarError("Calendar access wasn't granted - tick the calendar "
                            "permission on Google's screen.")
    if not tok.get("refresh_token"):
        raise CalendarError("Google didn't return lasting access - try again.")
    if not storage.write_secret(REFRESH_SECRET, tok["refresh_token"]):
        # Nowhere to keep it: don't report "connected" and go quiet an hour
        # later - give the grant back and say why.
        try:
            requests.post(REVOKE_URL, params={"token": tok["refresh_token"]},
                          timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            pass
        raise CalendarError("Couldn't save calendar access on this computer - "
                            "no system keyring is available.")
    _cache_token(tok)
    return _email_from_id_token(tok.get("id_token"))


def disconnect():
    """Forget the calendar here and revoke the app's access at Google."""
    refresh = ""
    try:
        refresh = storage.read_secret(REFRESH_SECRET) or ""
        storage.write_secret(REFRESH_SECRET, "")
    except Exception:
        pass
    with _token_lock:
        _token.update(value="", expires=0.0)
    if refresh:
        try:
            requests.post(REVOKE_URL, params={"token": refresh}, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            pass


# ── access token (memory only) ───────────────────────────────────────────────
_token = {"value": "", "expires": 0.0}
_token_lock = threading.Lock()


def _cache_token(tok):
    with _token_lock:
        _token["value"] = tok.get("access_token") or ""
        _token["expires"] = time.time() + float(tok.get("expires_in") or 3600) - 60


def _access_token(cfg=None):
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
    refresh = storage.read_secret(REFRESH_SECRET) or ""
    if not refresh:
        raise CalendarAuthError("Calendar isn't connected.")
    client_id, client_secret = _client(cfg)
    if not client_id:
        # Refreshing with an empty client id gets a 400 that is not a revoke.
        raise CalendarError("Calendar connection isn't set up in this version yet.")
    data = {"client_id": client_id, "refresh_token": refresh, "grant_type": "refresh_token"}
    if client_secret:
        data["client_secret"] = client_secret
    try:
        r = requests.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise CalendarError(f"Couldn't reach Google: {e}")
    if r.status_code in (400, 401):
        try:
            err = str((r.json() or {}).get("error") or "")
        except ValueError:
            err = ""
        if err == "invalid_grant":
            # Revoked, the password changed, or - while the Google app is
            # still in "Testing" - the 7-day token lifetime ran out.
            storage.write_secret(REFRESH_SECRET, "")
            raise CalendarAuthError("Calendar access expired - connect it again in Settings.")
        # invalid_client / invalid_request: our side is misconfigured - the
        # user's grant is fine, so keep it.
        raise CalendarError(f"Google refused the calendar refresh ({err or 'HTTP ' + str(r.status_code)}).")
    if r.status_code != 200:
        raise CalendarError(f"Google returned HTTP {r.status_code}.")
    _cache_token(r.json())
    with _token_lock:
        return _token["value"]


# ── events ───────────────────────────────────────────────────────────────────
def _rfc3339(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _name_from_email(email):
    local = (email or "").split("@")[0]
    words = [w for w in re.split(r"[._\-+]+", local) if w and not w.isdigit()]
    return " ".join(w.capitalize() for w in words)


def attendee_names(attendees):
    """People on the invite (you included - notes attribute owners by name),
    display name or a readable email; meeting rooms are left out."""
    names = []
    for a in attendees or []:
        if not isinstance(a, dict) or a.get("resource"):
            continue
        name = (a.get("displayName") or "").strip() or _name_from_email(a.get("email"))
        if name and name not in names:
            names.append(name)
    return names


def parse_event(item):
    """A Google event as a meeting dict, or None when it isn't a meeting you'd
    record: all-day, cancelled, declined by you, focus time / out of office."""
    if not isinstance(item, dict) or item.get("status") == "cancelled":
        return None
    if item.get("eventType") in _SKIP_EVENT_TYPES:
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    if not start.get("dateTime") or not end.get("dateTime"):
        return None                                   # all-day
    attendees = item.get("attendees") or []
    me = next((a for a in attendees if isinstance(a, dict) and a.get("self")), None)
    if me and me.get("responseStatus") == "declined":
        return None
    try:
        st, en = _parse_dt(start["dateTime"]), _parse_dt(end["dateTime"])
    except (ValueError, TypeError):
        return None
    link = item.get("hangoutLink") or next(
        (e.get("uri") for e in (item.get("conferenceData") or {}).get("entryPoints") or []
         if isinstance(e, dict) and e.get("entryPointType") == "video"), "")
    others = attendee_names([a for a in attendees if not (isinstance(a, dict) and a.get("self"))])
    return {
        "id": item.get("id") or f"{st.isoformat()}|{item.get('summary', '')}",
        "title": (item.get("summary") or "Untitled meeting").strip(),
        "start": st,
        "end": en,
        "attendees": attendee_names(attendees),
        "others": others,
        "link": link or "",
    }


def upcoming(cfg=None, hours=12, past_minutes=60, now=None):
    """Meetings from an hour ago to ``hours`` ahead, soonest first."""
    now = now or datetime.now(timezone.utc)
    params = {"timeMin": _rfc3339(now - timedelta(minutes=past_minutes)),
              "timeMax": _rfc3339(now + timedelta(hours=hours)),
              "singleEvents": "true", "orderBy": "startTime", "maxResults": "50"}
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_access_token(cfg)}"}
        try:
            r = requests.get(EVENTS_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise CalendarError(f"Couldn't reach Google Calendar: {e}")
        if r.status_code == 401 and attempt == 1:
            with _token_lock:                          # stale access token: refresh once
                _token.update(value="", expires=0.0)
            continue
        if r.status_code != 200:
            raise CalendarError(f"Google Calendar returned HTTP {r.status_code}.")
        break
    return [m for m in (parse_event(i) for i in r.json().get("items") or []) if m]


def should_prompt(meeting, now, lead_seconds=60, grace_seconds=300):
    """Ask from a minute before the start until five minutes in - not for a
    meeting that's well underway or over."""
    start, end = meeting["start"], meeting["end"]
    return (start - timedelta(seconds=lead_seconds) <= now
            <= min(end, start + timedelta(seconds=grace_seconds)))


def current_meeting(meetings, now, lead_seconds=300):
    """The meeting happening now (or starting within ``lead_seconds``), for
    filling in the Record Meeting form. None if there isn't one."""
    live = [m for m in meetings
            if m["start"] - timedelta(seconds=lead_seconds) <= now < m["end"]]
    return min(live, key=lambda m: abs((m["start"] - now).total_seconds())) if live else None
