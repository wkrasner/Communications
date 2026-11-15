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


def telnyx(ccid, action, body, event_id=""):
    """Send a call command. Returns True on success; never raises."""
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
        print(f"telnyx {action} failed {e.code}: {e.read()[:300]!r}")
    except Exception as e:  # network errors, timeouts
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

def ring_softphone(a_ccid, caller, label, a_answered, event_id):
    body = {
        "to": RING_TARGET,
        "timeout_secs": RING_SECS,
        "from_display_name": label[:60],
        # state for the NEW leg, so its answer/hangup tells us what to do with leg A
        "client_state": encode_state({"s": "ring", "a": a_ccid, "aa": 1 if a_answered else 0}),
    }
    if caller:
        body["from"] = caller
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
    line = LINES.get(p.get("to", ""), "Other")
    entry = db_get(f"NUM#{caller}") if caller else None
    status = (entry or {}).get("status", "unknown")
    name = (entry or {}).get("name", "")
    pk = f"CALL#{p['call_session_id']}"
    ts = now_iso()
    record = {
        "pk": pk, "sk": "META", "gsi1pk": "CALLS", "gsi1sk": ts,
        "ts": ts, "from": caller or "anonymous", "name": name, "line": line,
        "list": status, "answered": False,
    }
    label = f"{line}: {name}" if name else line

    if status == "block":
        record["result"] = "blocked"
        db_put(record)
        telnyx(ccid, "reject", {"cause": "CALL_REJECTED"}, event_id)
    elif status == "allow":
        record["result"] = "missed"
        db_put(record)
        if not ring_softphone(ccid, caller, label, False, event_id):
            db_update(pk, result="ring_failed")
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
        db_update(f"CALL#{p['call_session_id']}", answered=True, result="answered")


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
        caller = rec.get("from", "")
        ok = ring_softphone(ccid, caller if caller.startswith("+") else "",
                            f"{rec.get('line', 'Other')}: screened", True, event_id)
        if not ok:
            # The softphone could not be reached at all (not registered, bad
            # credentials). No leg B exists, so no hangup webhook is coming.
            db_update(pk, result="ring_failed")
            start_voicemail_greeting(ccid, event_id)
    else:
        db_update(pk, result="screen_failed", screen="failed")
        start_voicemail_greeting(ccid, event_id)


def on_speak_ended(p, state, event_id):
    if state.get("s") != "vmrec":
        return
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
    key = f"voicemail/{session}.mp3"
    save_voicemail(url, key)
    db_update(f"CALL#{session}", voicemail=key, vm_ts=now_iso())


def handle_webhook(body):
    data = body.get("data", {})
    etype = data.get("event_type", "")
    p = data.get("payload", {})
    event_id = data.get("id", "")
    state = decode_state(p.get("client_state"))
    if etype == "call.initiated":
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


def lambda_handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "")
    headers = event.get("headers") or {}
    raw = event.get("body") or ""
    raw_bytes = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if path == "/telnyx" and method == "POST":
        if not verify_signature(raw_bytes, headers):
            return respond(401, {"error": "bad signature"})
        try:
            handle_webhook(json.loads(raw_bytes))
        except Exception as e:  # always 200 so Telnyx does not retry into a live call
            print(f"webhook error: {e!r}")
        return respond(200, {"ok": True})

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
