"""Google Voice texts over Gmail.

Google Voice can forward each incoming text to the account's email, and a
reply to that email goes back out as a text from the Google Voice number.
That is the only route available while the number still lives at Google, so
this module reads those notifications over IMAP and sends replies over SMTP.

Gmail is reached with an app password rather than OAuth: no cloud project,
no refresh tokens. The password needs 2-Step Verification turned on.
"""
import email
import imaplib
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
GV_DOMAIN = "txt.voice.google.com"

# Trailing boilerplate Google appends under the message itself.
_CUT = re.compile(
    r"(?im)^\s*(?:--+\s*$|to respond to this text|this message was sent|"
    r"you(?:'re| are) receiving this|https?://(?:www\.)?google\.com/voice|"
    r"YOUR ACCOUNT\b|Sent from Google Voice)"
)
_PHONE = re.compile(r"\+?1?[\s.-]?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})")


def _decode(raw):
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return raw or ""


def _body(msg):
    """The plain-text part, preferring text/plain over a stripped HTML part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def _sender_number(msg, body):
    """The number that sent the text, from whichever header carries it."""
    for field in (_decode(msg.get("Subject")), _decode(msg.get("From")), body[:200]):
        m = _PHONE.search(field or "")
        if m:
            return "+1" + "".join(m.groups())
    return ""


def _sender_name(msg):
    frm = _decode(msg.get("From"))
    name = frm.split("<")[0].strip().strip('"')
    # Google puts the contact's name here, or the number when it has no name.
    name = re.sub(r"\s*\(SMS\)\s*$", "", name)
    return "" if _PHONE.fullmatch(name.strip()) else name[:80]


def parse(msg):
    """A Google Voice notification email as a text message, or None."""
    reply_to = _decode(msg.get("Reply-To")) or _decode(msg.get("From"))
    m = re.search(r"[\w.+-]+@" + re.escape(GV_DOMAIN), reply_to)
    if not m:
        return None
    body = _body(msg)
    cut = _CUT.search(body)
    text = (body[: cut.start()] if cut else body).strip()
    return {
        "relay": m.group(0),
        "from": _sender_number(msg, body),
        "name": _sender_name(msg),
        "text": text[:1500],
        "raw": body[:1500],  # kept so a mis-parse can be seen and fixed
        "date": _decode(msg.get("Date")),
        "message_id": _decode(msg.get("Message-ID")),
    }


def fetch(user, password, since_uid=0, limit=50):
    """New Google Voice notifications since `since_uid`.

    Returns (messages, highest_uid). UID tracking rather than the unread flag,
    so reading a text in Gmail first does not make it vanish from here.
    """
    out, highest = [], since_uid
    conn = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ssl.create_default_context())
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        criteria = f"UID {since_uid + 1}:*" if since_uid else "ALL"
        typ, data = conn.uid("SEARCH", None, criteria, "FROM", GV_DOMAIN)
        if typ != "OK":
            return out, highest
        uids = [int(u) for u in (data[0] or b"").split()]
        uids = [u for u in uids if u > since_uid][-limit:]
        for uid in uids:
            typ, raw = conn.uid("FETCH", str(uid), "(RFC822)")
            highest = max(highest, uid)
            if typ != "OK" or not raw or not raw[0]:
                continue
            parsed = parse(email.message_from_bytes(raw[0][1]))
            if parsed:
                parsed["uid"] = uid
                out.append(parsed)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out, highest


def send(user, password, relay, text):
    """Reply to a Google Voice relay address; Google turns it into a text."""
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = relay
    msg["Subject"] = "Re:"
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)
