"""Personal phone screen on Telnyx Voice API.

One Lambda behind an HTTP API:
  POST /telnyx          Telnyx Voice API webhooks (signature-verified)
  GET  /admin           admin page (allow/block lists, call log, voicemail)
  *    /admin/api/...   admin JSON API (x-admin-token header)

Call flow
  blocked caller   -> rejected
  allowed caller   -> rings the softphone; no answer -> voicemail
  unknown caller   -> "press 1" challenge; pass -> rings softphone; fail -> voicemail
"""
import base64
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TELNYX_API = "https://api.telnyx.com/v2"
API_KEY = os.environ.get("TELNYX_API_KEY", "")
PUBLIC_KEY = os.environ.get("TELNYX_PUBLIC_KEY", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
RING_TARGET = os.environ.get("RING_TARGET", "")  # sip:username@sip.telnyx.com
RING_SECS = int(os.environ.get("RING_SECS", "25"))
VOICE = os.environ.get("VOICE", "female")
LANGUAGE = os.environ.get("LANGUAGE", "en-US")
TABLE_NAME = os.environ.get("TABLE_NAME", "")
BUCKET = os.environ.get("VOICEMAIL_BUCKET", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET", "")
GATEWAY_USER = os.environ.get("SMS_GATEWAY_USER", "")
GATEWAY_PASS = os.environ.get("SMS_GATEWAY_PASS", "")
GATEWAY_API = "https://api.sms-gate.app/3rdparty/v1/messages"
# SimpApp's SMS Gateway API (the Play Store one): bearer key, different shape.
SMS_API_KEY = os.environ.get("SMS_API_KEY", "")
SMS_API_URL = os.environ.get(
    "SMS_API_URL",
    "https://europe-west1-sms-gateway-api-simpapp.cloudfunctions.net/api_v2_sms_send",
)
OFFICE_PHONE = os.environ.get("OFFICE_PHONE", "")
CELL_NUMBER = os.environ.get("CELL_NUMBER", "")  # the real cell, for labelling
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")
# Answered calls are recorded only when this is on. Recording consent law
# varies by state, so it is a deliberate switch rather than a default.
RECORD_CALLS = os.environ.get("RECORD_CALLS", "off").lower() in ("on", "true", "1")
# Telnyx DID -> label for the line that forwards to it
LINES = {
    os.environ.get("CELL_DID", ""): "Cell",
    os.environ.get("GV_DID", ""): "GV",
}

SCREEN_PROMPT = (
    "This call is being screened. Press 1 to be connected. "
    "Otherwise, stay on the line to leave a message."
)
VM_GREETING = "Please leave a message after the tone."

_table = None
_s3 = None


# ---------- storage ----------

def table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def db_get(pk):
    return table().get_item(Key={"pk": pk, "sk": "META"}).get("Item")


def db_put(item):
    table().put_item(Item=item)


def db_update(pk, **attrs):
    names = {f"#k{i}": k for i, k in enumerate(attrs)}
    values = {f":v{i}": v for i, v in enumerate(attrs.values())}
    expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
    table().update_item(
        Key={"pk": pk, "sk": "META"},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def db_delete(pk):
    table().delete_item(Key={"pk": pk, "sk": "META"})


def db_list(kind, limit=200):
    from boto3.dynamodb.conditions import Key
    resp = table().query(
        IndexName="byTime",
        KeyConditionExpression=Key("gsi1pk").eq(kind),
        ScanIndexForward=False,
        Limit=limit,
    )
    return resp.get("Items", [])


def save_voicemail(url, key):
    with urllib.request.urlopen(url, timeout=15) as r:
        s3().put_object(Bucket=BUCKET, Key=key, Body=r.read(), ContentType="audio/mpeg")


def voicemail_url(key):
    return s3().generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=600
    )


def read_audio(key):
    return s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()


def transcribe(audio, filename="audio.mp3"):
    """Whisper transcript of recorded audio. Returns '' rather than raising."""
    if not OPENAI_KEY:
        return ""
    boundary = "----communications" + base64.urlsafe_b64encode(os.urandom(9)).decode()
    parts = []
    for name, value in (("model", OPENAI_MODEL), ("response_format", "text")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: audio/mpeg\r\n\r\n'.encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        OPENAI_URL, data=b"".join(parts), method="POST",
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=110) as r:
            return r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        print(f"transcribe failed {e.code}: {e.read()[:300]!r}")
    except Exception as e:
        print(f"transcribe error: {e}")
    return ""


def transcribe_later(pk, key, field):
    """Hand the transcription to a second invocation so the webhook returns now."""
    try:
        import boto3
        boto3.client("lambda").invoke(
            FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
            InvocationType="Event",
            Payload=json.dumps({"transcribe": {"pk": pk, "key": key, "field": field}}).encode(),
        )
    except Exception as e:
        print(f"could not queue transcription: {e}")


# ---------- helpers ----------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize(number):
    """US-centric E.164. Returns '' for anonymous/unparseable callers."""
    digits = re.sub(r"\D", "", number or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) > 11 and (number or "").strip().startswith("+"):
        return "+" + digits
    return ""


def encode_state(d):
    return base64.b64encode(json.dumps(d).encode()).decode()


def decode_state(s):
    if not s:
        return {}
    try:
        return json.loads(base64.b64decode(s))
    except Exception:
        return {}


LAST_ERROR = {}


def telnyx(ccid, action, body, event_id=""):
    """Send a call command. Returns True on success; never raises.

    Failures are recorded in LAST_ERROR so the caller can store the reason
    on the call record — Telnyx refusals are otherwise invisible from here.
    """
    if event_id:
        # Telnyx ignores repeat commands with the same command_id on a call,
        # which makes webhook redelivery safe.
        body = dict(body, command_id=f"{event_id}-{action}")
    req = urllib.request.Request(
        f"{TELNYX_API}/calls/{ccid}/actions/{action}",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        LAST_ERROR[ccid] = f"{action} {e.code}: {detail}"
        print(f"telnyx {LAST_ERROR[ccid]}")
    except Exception as e:  # network errors, timeouts
        LAST_ERROR[ccid] = f"{action}: {e}"
        print(f"telnyx {action} error: {e}")
    return False


def verify_signature(raw, headers):
    sig = headers.get("telnyx-signature-ed25519")
    ts = headers.get("telnyx-timestamp")
    if not sig or not ts or not PUBLIC_KEY:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
        from nacl.signing import VerifyKey
        VerifyKey(base64.b64decode(PUBLIC_KEY)).verify(
            ts.encode() + b"|" + raw, base64.b64decode(sig)
        )
        return True
    except Exception:
        return False


# ---------- call flow ----------

def ring_softphone(a_ccid, did, label, a_answered, event_id):
    """Ring the softphone.

    `from` must be a number on the account — presenting the original
    caller's number is rejected as unauthorized caller ID — so the DID the
    call arrived on is used, and who is calling goes in the display name.
    """
    body = {
        "to": RING_TARGET,
        "timeout_secs": RING_SECS,
        "from_display_name": label[:60],
        # state for the NEW leg, so its answer/hangup tells us what to do with leg A
        "client_state": encode_state({"s": "ring", "a": a_ccid, "aa": 1 if a_answered else 0}),
    }
    if did:
        body["from"] = did
    return telnyx(a_ccid, "transfer", body, event_id)


def start_voicemail_greeting(ccid, event_id):
    return telnyx(ccid, "speak", {
        "payload": VM_GREETING,
        "voice": VOICE,
        "language": LANGUAGE,
        "client_state": encode_state({"s": "vmrec"}),
    }, event_id)


def on_initiated(p, event_id):
    if p.get("direction") != "incoming":
        return  # the leg we created toward the softphone
    ccid = p["call_control_id"]
    caller = normalize(p.get("from"))
    did = p.get("to", "")
    line = LINES.get(did, "Other")
    entry = db_get(f"NUM#{caller}") if caller else None
    status = (entry or {}).get("status", "unknown")
    name = (entry or {}).get("name", "")
    pk = f"CALL#{p['call_session_id']}"
    ts = now_iso()
    record = {
        "pk": pk, "sk": "META", "gsi1pk": "CALLS", "gsi1sk": ts,
        "ts": ts, "from": caller or "anonymous", "name": name, "line": line,
        "did": did, "list": status, "answered": False,
    }
    label = f"{line} {name or caller or 'unknown'}"

    if status == "block":
        record["result"] = "blocked"
        db_put(record)
        telnyx(ccid, "reject", {"cause": "CALL_REJECTED"}, event_id)
    elif status == "allow":
        record["result"] = "missed"
        db_put(record)
        if not ring_softphone(ccid, did, label, False, event_id):
            db_update(pk, result="ring_failed", error=LAST_ERROR.get(ccid, "")[:400])
            telnyx(ccid, "answer", {"client_state": encode_state({"s": "vm"})}, event_id)
    else:
        record["result"] = "screening"
        db_put(record)
        telnyx(ccid, "answer", {"client_state": encode_state({"s": "screen"})}, event_id)


def on_answered(p, state, event_id):
    ccid = p["call_control_id"]
    s = state.get("s")
    if s == "screen":
        telnyx(ccid, "gather_using_speak", {
            "payload": SCREEN_PROMPT,
            "voice": VOICE,
            "language": LANGUAGE,
            "valid_digits": "1",
            "minimum_digits": 1,
            "maximum_digits": 1,
            "timeout_millis": 8000,
            "client_state": encode_state({"s": "screen"}),
        }, event_id)
    elif s == "vm":
        start_voicemail_greeting(ccid, event_id)
    elif s == "ring":
        pk = f"CALL#{p['call_session_id']}"
        db_update(pk, answered=True, result="answered")
        if RECORD_CALLS:
            # Record on leg A so both sides are captured.
            a_ccid = state.get("a")
            if a_ccid:
                db_update(pk, recording_kind="call")
                telnyx(a_ccid, "record_start", {
                    "format": "mp3", "channels": "dual", "trim": "trim-silence",
                }, event_id)


def on_gather_ended(p, state, event_id):
    if state.get("s") != "screen":
        return
    ccid = p["call_control_id"]
    pk = f"CALL#{p['call_session_id']}"
    if p.get("status") == "call_hangup":
        db_update(pk, result="screen_failed")
        return
    if p.get("digits") == "1":
        db_update(pk, result="missed", screen="passed")
        rec = db_get(pk) or {}
        label = f"{rec.get('line', 'Other')} {rec.get('from', 'unknown')}"
        if not ring_softphone(ccid, rec.get("did", ""), label, True, event_id):
            # The softphone could not be reached at all (not registered, bad
            # credentials). No leg B exists, so no hangup webhook is coming.
            db_update(pk, result="ring_failed", error=LAST_ERROR.get(ccid, "")[:400])
            start_voicemail_greeting(ccid, event_id)
    else:
        db_update(pk, result="screen_failed", screen="failed")
        start_voicemail_greeting(ccid, event_id)


def on_speak_ended(p, state, event_id):
    if state.get("s") != "vmrec":
        return
    db_update(f"CALL#{p['call_session_id']}", recording_kind="voicemail")
    telnyx(p["call_control_id"], "record_start", {
        "format": "mp3",
        "channels": "single",
        "play_beep": True,
        "max_length": 180,
        "timeout_secs": 6,
    }, event_id)


def on_hangup(p, state, event_id):
    pk = f"CALL#{p['call_session_id']}"
    if state.get("s") != "ring":
        return
    # The softphone leg ended. If it was never answered, send the caller to voicemail.
    rec = db_get(pk) or {}
    if rec.get("answered"):
        return
    a_ccid = state.get("a")
    if not a_ccid:
        return
    if state.get("aa"):
        start_voicemail_greeting(a_ccid, event_id)
    else:
        telnyx(a_ccid, "answer", {"client_state": encode_state({"s": "vm"})}, event_id)


def on_recording_saved(p, event_id):
    urls = p.get("recording_urls") or p.get("public_recording_urls") or {}
    url = urls.get("mp3") or urls.get("wav")
    if not url:
        return
    session = p["call_session_id"]
    pk = f"CALL#{session}"
    kind = (db_get(pk) or {}).get("recording_kind", "voicemail")
    if kind == "call":
        key = f"calls/{session}.mp3"
        save_voicemail(url, key)
        db_update(pk, recording=key, rec_ts=now_iso(), transcript="(transcribing…)")
        transcribe_later(pk, key, "transcript")
    else:
        key = f"voicemail/{session}.mp3"
        save_voicemail(url, key)
        db_update(pk, voicemail=key, vm_ts=now_iso(), vm_transcript="(transcribing…)")
        transcribe_later(pk, key, "vm_transcript")


def run_transcription(job):
    pk, key, field = job.get("pk"), job.get("key"), job.get("field", "transcript")
    text = transcribe(read_audio(key), os.path.basename(key))
    db_update(pk, **{field: text or "(no transcript)"})
    return {"pk": pk, "chars": len(text)}


# Words that suggest the text is a clinical question rather than ordinary chat.
MEDICAL = re.compile(
    r"(?i)\b(pain|hurts?|fever|chest|breath(?:ing|less)?|dizzy|nause|vomit|"
    r"rash|swell(?:ing|ed)?|infect|antibiotic|refill|prescription|pharmacy|"
    r"\brx\b|dose|dosage|mg\b|blood pressure|\bbp\b|sugar|glucose|insulin|"
    r"lab(?:s| work| result)|x-?ray|mri|\bct\b|scan|biopsy|appointment|"
    r"referral|symptom|diagnos|bleeding|headache|cough|sore throat|"
    r"urgent care|\ber\b|emergency room|sick|injur)"
)


def office_reply():
    if not OFFICE_PHONE:
        return ""
    return (
        f"For anything medical, please call or text the office at {OFFICE_PHONE} "
        "so it goes in your chart and reaches the right person. "
        "If this is an emergency, call 911."
    )


def store_message(ident, caller, did, line, text, direction="in", src="telnyx",
                  relay="", name="", media=0):
    """One text, inbound or outbound, in the thread for `caller`."""
    caller = normalize(caller) or caller or "unknown"
    entry = db_get(f"NUM#{caller}") if caller.startswith("+") else None
    ts = now_iso()
    item = {
        "pk": f"MSG#{ident}", "sk": "META", "gsi1pk": "MSGS", "gsi1sk": ts,
        "ts": ts, "contact": caller, "from": caller,
        "name": (entry or {}).get("name") or name, "line": line, "did": did,
        "dir": direction, "src": src, "text": (text or "")[:1500], "media": media,
    }
    if relay:
        item["relay"] = relay
    if direction == "in" and text and MEDICAL.search(text):
        item["medical"] = True
    db_put(item)
    return item


def latest_thread_route(contact):
    """How to reach `contact`: the most recent inbound message decides."""
    msgs = [m for m in db_list("MSGS", 400) if m.get("contact") == contact]
    msgs.sort(key=lambda m: m.get("ts", ""), reverse=True)
    for m in msgs:
        if m.get("dir") == "in":
            return m.get("src", "cell"), m.get("relay", "")
    return ("cell", "")


def send_text(contact, text):
    """Send as whichever of his numbers the thread belongs to."""
    src, relay = latest_thread_route(contact)
    if src == "gv":
        if not (GMAIL_USER and GMAIL_APP_PASSWORD and relay):
            return False, "no Gmail credentials or no relay address for this thread"
        import gvmail
        gvmail.send(GMAIL_USER, GMAIL_APP_PASSWORD, relay, text)
        store_message(f"out-{now_iso()}", contact, "", "GV", text,
                      direction="out", src="gv", relay=relay)
        return True, "sent from Google Voice"
    if SMS_API_KEY:
        url = SMS_API_URL
        body = json.dumps({"phoneNumber": contact, "message": text}).encode()
        auth = f"Bearer {SMS_API_KEY}"
    elif GATEWAY_USER and GATEWAY_PASS:
        url = GATEWAY_API
        body = json.dumps({"message": text, "phoneNumbers": [contact]}).encode()
        auth = "Basic " + base64.b64encode(
            f"{GATEWAY_USER}:{GATEWAY_PASS}".encode()).decode()
    else:
        return False, "no SMS gateway credentials"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        return False, f"gateway {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:
        return False, f"gateway error: {e}"
    store_message(f"out-{now_iso()}", contact, "", "Cell", text,
                  direction="out", src="cell")
    return True, "sent from your cell"


def poll_gmail():
    """Pull new Google Voice notification emails into the message store."""
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        return {"polled": 0, "note": "no Gmail credentials set"}
    import gvmail
    cursor = db_get("STATE#gvuid") or {}
    since = int(cursor.get("uid", 0))
    msgs, highest = gvmail.fetch(GMAIL_USER, GMAIL_APP_PASSWORD, since)
    for m in msgs:
        store_message(f"gv-{m['uid']}", m["from"], os.environ.get("GV_DID", ""),
                      "GV", m["text"], src="gv", relay=m["relay"], name=m["name"])
    if highest > since:
        db_put({"pk": "STATE#gvuid", "sk": "META", "uid": highest})
    return {"polled": len(msgs), "uid": highest}


def on_message_received(p):
    """Store an inbound SMS/MMS so it shows in the panel."""
    frm = p.get("from") or {}
    dids = [t.get("phone_number", "") for t in (p.get("to") or [])]
    did = next((d for d in dids if d in LINES), dids[0] if dids else "")
    store_message(p.get("id", now_iso()), frm.get("phone_number", ""), did,
                  LINES.get(did, "Other"), p.get("text", ""), src="telnyx",
                  media=len(p.get("media") or []))


def handle_webhook(body):
    data = body.get("data", {})
    etype = data.get("event_type", "")
    p = data.get("payload", {})
    event_id = data.get("id", "")
    state = decode_state(p.get("client_state"))
    if etype == "message.received":
        on_message_received(p)
    elif etype == "call.initiated":
        on_initiated(p, event_id)
    elif etype == "call.answered":
        on_answered(p, state, event_id)
    elif etype == "call.gather.ended":
        on_gather_ended(p, state, event_id)
    elif etype == "call.speak.ended":
        on_speak_ended(p, state, event_id)
    elif etype == "call.hangup":
        on_hangup(p, state, event_id)
    elif etype == "call.recording.saved":
        on_recording_saved(p, event_id)


# ---------- admin ----------

def clean(item):
    return {k: v for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")}


def admin_api(method, path, query, body):
    if path == "/admin/api/calls" and method == "GET":
        return 200, [clean(i) for i in db_list("CALLS", 100)]
    if path == "/admin/api/messages" and method == "GET":
        return 200, [clean(i) for i in db_list("MSGS", 300)]
    if path == "/admin/api/send" and method == "POST":
        to = normalize(body.get("to"))
        text = (body.get("text") or "").strip()
        if not to or not text:
            return 400, {"error": "need a number and some text"}
        ok, note = send_text(to, text)
        return (200 if ok else 502), {"ok": ok, "note": note}
    if path == "/admin/api/office-reply" and method == "GET":
        return 200, {"text": office_reply()}
    if path == "/admin/api/poll" and method == "POST":
        return 200, poll_gmail()
    if path == "/admin/api/numbers" and method == "GET":
        return 200, [clean(i) for i in db_list("NUMS", 1000)]
    if path == "/admin/api/numbers" and method == "POST":
        saved, skipped = 0, []
        for row in body.get("numbers", []):
            num = normalize(row.get("number"))
            status = row.get("status")
            if not num or status not in ("allow", "block"):
                skipped.append(row.get("number"))
                continue
            db_put({
                "pk": f"NUM#{num}", "sk": "META", "gsi1pk": "NUMS", "gsi1sk": num,
                "number": num, "status": status, "name": (row.get("name") or "")[:80],
            })
            saved += 1
        return 200, {"saved": saved, "skipped": skipped}
    if path == "/admin/api/numbers" and method == "DELETE":
        num = normalize(query.get("number"))
        if not num:
            return 400, {"error": "bad number"}
        db_delete(f"NUM#{num}")
        return 200, {"deleted": num}
    if path == "/admin/api/voicemail" and method == "GET":
        key = query.get("key", "")
        if not re.fullmatch(r"voicemail/[\w-]+\.mp3", key):
            return 400, {"error": "bad key"}
        return 200, {"url": voicemail_url(key)}
    return 404, {"error": "not found"}


def respond(code, body, content_type="application/json"):
    return {
        "statusCode": code,
        "headers": {"content-type": content_type, "cache-control": "no-store"},
        "body": body if isinstance(body, str) else json.dumps(body, default=str),
    }


def on_gateway_post(body):
    """Inbound text from the gateway app on the Android phone.

    Two payload shapes are accepted so either app works: SimpApp's
    {"type": "incoming_sms", "sender", "message"} and capcom6's
    {"payload": {"phoneNumber", "message"}}.
    """
    if body.get("type") and body["type"] != "incoming_sms":
        return  # delivery_status and friends: nothing to file
    p = body.get("payload") or body
    sender = p.get("sender") or p.get("phoneNumber") or ""
    text = p.get("message", "")
    media = len(p.get("attachments") or p.get("media") or [])
    store_message(body.get("id") or p.get("messageId") or now_iso(),
                  sender, CELL_NUMBER, "Cell", text, src="cell", media=media)


def lambda_handler(event, context):
    # Background invocations rather than HTTP requests.
    if event.get("poll"):
        return poll_gmail()
    if event.get("transcribe"):
        return run_transcription(event["transcribe"])

    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "")
    headers = event.get("headers") or {}
    raw = event.get("body") or ""
    raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if path in ("/telnyx", "/gateway") and method == "POST":
        # Either URL accepts either sender, so pointing Telnyx at /gateway or
        # the phone app at /telnyx still works. What decides is how the request
        # authenticates, and the payload decides how it is read.
        signed = verify_signature(raw_bytes, headers)
        secret = (headers.get("x-gateway-secret")
                  or (event.get("queryStringParameters") or {}).get("k") or "")
        shared = bool(GATEWAY_SECRET) and hmac.compare_digest(secret, GATEWAY_SECRET)
        if not (signed or shared):
            return respond(401, {"error": "unauthorized"})
        try:
            body = json.loads(raw_bytes)
            if signed and isinstance(body.get("data"), dict):
                handle_webhook(body)
            else:
                on_gateway_post(body)
        except Exception as e:  # always 200 so Telnyx does not retry into a live call
            print(f"webhook error on {path}: {e!r}")
        return respond(200, {"ok": True})

    if path == "/manifest.webmanifest" and method == "GET":
        # Lets Android install the panel to the home screen as an app.
        return respond(200, {
            "name": "Communications", "short_name": "Comms",
            "start_url": "/admin", "scope": "/admin", "display": "standalone",
            "background_color": "#ffffff", "theme_color": "#2563eb",
            "icons": [{
                "src": ("data:image/svg+xml,"
                        "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'%3E"
                        "%3Crect width='192' height='192' rx='40' fill='%232563eb'/%3E"
                        "%3Ctext x='96' y='130' font-size='104' text-anchor='middle'"
                        " fill='white' font-family='sans-serif'%3EC%3C/text%3E%3C/svg%3E"),
                "sizes": "192x192", "type": "image/svg+xml", "purpose": "any",
            }],
        }, "application/manifest+json")

    if path == "/admin" and method == "GET":
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "admin.html"), encoding="utf-8") as f:
            return respond(200, f.read(), "text/html; charset=utf-8")

    if path.startswith("/admin/api/"):
        token = headers.get("x-admin-token", "")
        if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
            return respond(401, {"error": "unauthorized"})
        try:
            body = json.loads(raw_bytes) if raw_bytes else {}
        except ValueError:
            return respond(400, {"error": "bad json"})
        code, out = admin_api(method, path, event.get("queryStringParameters") or {}, body)
        return respond(code, out)

    return respond(404, {"error": "not found"})
