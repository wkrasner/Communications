"""Reading a Gmail mailbox over IMAP, and replying over SMTP.

Separate from gvmail.py, which parses Google Voice notifications. This one is
for ordinary mail: the personal account in full, and the practice account as
headers only — subject and sender, never the body — because patient mail
should not land in this app while its only credential is a shared token.
"""
import email
import imaplib
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Mail that is announcements rather than correspondence. A personal comms
# panel filled with receipts and newsletters is no use to anyone.
BULK_SENDER = re.compile(
    r"(?i)(no[-._]?reply|do[-._]?not[-._]?reply|notifications?@|mailer@|"
    r"bounce|postmaster@|newsletter|marketing@|updates?@)")


def _decode(raw):
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return raw or ""


def _body(msg, limit=4000):
    """The plain-text part. HTML is crudely stripped only if there is nothing else."""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if "attachment" in str(part.get("Content-Disposition", "")):
                continue
            kind = part.get_content_type()
            if kind not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            if kind == "text/plain":
                return text[:limit]
            html = html or text
    else:
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        if msg.get_content_type() == "text/plain":
            return text[:limit]
        html = text
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()[:limit]


def is_bulk(msg):
    """Newsletters and machine mail, by the headers that mark them."""
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    if str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk"):
        return True
    if str(msg.get("Auto-Submitted", "")).lower().startswith("auto"):
        return True
    return bool(BULK_SENDER.search(_decode(msg.get("From"))))


def parse(msg, want_body):
    frm = getaddresses([_decode(msg.get("From"))])
    name, addr = (frm[0] if frm else ("", ""))
    try:
        ts = parsedate_to_datetime(msg.get("Date")).astimezone().isoformat()
    except Exception:
        ts = ""
    to = [a for _, a in getaddresses([_decode(msg.get("To"))]) if a]
    out = {
        "from_addr": (addr or "").lower(),
        "from_name": (name or "").strip()[:80],
        "subject": _decode(msg.get("Subject"))[:300],
        "ts": ts,
        "to": to[:5],
        "message_id": _decode(msg.get("Message-ID"))[:200],
        "bulk": is_bulk(msg),
    }
    # The practice mailbox stops here: a subject line is not a chart note.
    out["body"] = _body(msg) if want_body else ""
    return out


def fetch(user, password, since_uid=0, want_body=True, limit=60, lookback_days=30):
    """New mail since `since_uid`. Returns (messages, highest_uid)."""
    out, highest = [], since_uid
    conn = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ssl.create_default_context())
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        if since_uid:
            criteria = (f"UID {since_uid + 1}:*",)
        else:
            # First run: a month, not the whole mailbox.
            import datetime
            day = (datetime.date.today()
                   - datetime.timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            criteria = ("SINCE", day)
        typ, data = conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            return out, highest
        uids = [int(u) for u in (data[0] or b"").split()]
        uids = [u for u in uids if u > since_uid][-limit:]
        for uid in uids:
            typ, raw = conn.uid("FETCH", str(uid), "(RFC822)")
            highest = max(highest, uid)
            if typ != "OK" or not raw or not raw[0]:
                continue
            item = parse(email.message_from_bytes(raw[0][1]), want_body)
            item["uid"] = uid
            out.append(item)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out, highest


def send(user, password, to, subject, text, in_reply_to=""):
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)
