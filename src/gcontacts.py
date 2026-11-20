"""Google Contacts over the People API.

Names for callers, kept current without exporting a CSV by hand. This needs
real OAuth — an app password reaches mail and nothing else — so the flow is:
the panel hands you a consent URL, Google redirects back to /oauth/callback,
and the refresh token it returns is stored and reused from then on.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PEOPLE_URL = "https://people.googleapis.com/v1/people/me/connections"
SCOPE = "https://www.googleapis.com/auth/contacts.readonly"


def _post(url, data):
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def consent_url(client_id, redirect_uri, state):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without this, a second authorisation returns no refresh token at all.
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    })


def exchange_code(client_id, client_secret, redirect_uri, code):
    """Swap the one-time code for a refresh token."""
    return _post(TOKEN_URL, {
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})


def access_token(client_id, client_secret, refresh_token):
    return _post(TOKEN_URL, {
        "refresh_token": refresh_token, "client_id": client_id,
        "client_secret": client_secret, "grant_type": "refresh_token"})["access_token"]


def _get(url, token, attempts=3):
    """GET with the error body surfaced, and a retry for Google's 5xx blips."""
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            last = RuntimeError(f"People API {e.code}: {detail}")
            if e.code < 500:
                raise last
            time.sleep(1 + attempt)
    raise last


def connections(token, page_limit=20):
    """Every contact with a phone number, as (number, name) pairs."""
    out, page, pages = [], None, 0
    while pages < page_limit:
        # Deliberately plain: no sort order, a modest page size. Both have
        # been seen to provoke 500s from this endpoint.
        params = {"personFields": "names,phoneNumbers,organizations",
                  "pageSize": 200}
        if page:
            params["pageToken"] = page
        body = _get(PEOPLE_URL + "?" + urllib.parse.urlencode(params), token)
        for person in body.get("connections", []):
            names = person.get("names") or []
            orgs = person.get("organizations") or []
            name = (names[0].get("displayName") if names else "") or \
                   (orgs[0].get("name") if orgs else "")
            for phone in person.get("phoneNumbers") or []:
                number = phone.get("canonicalForm") or phone.get("value") or ""
                if number:
                    out.append((number, (name or "").strip()[:80]))
        page = body.get("nextPageToken")
        pages += 1
        if not page:
            break
    return out
