"""Simulates Telnyx webhook sequences against the handler with fake storage and a fake Telnyx."""
import hashlib
import hmac
import io
import json
import os
import re
import sys
import unittest

os.environ.update(CELL_DID="+16095550001", GV_DID="+16095550002",
                  RING_TARGET="sip:me@sip.telnyx.com", ADMIN_TOKEN="secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import handler as h  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.db, self.cmds, self.saved = {}, [], []
        self.fail_actions = set()
        h.db_get = lambda pk: self.db.get(pk)
        h.db_put = lambda item: self.db.__setitem__(item["pk"], dict(item))
        h.db_update = lambda pk, **a: self.db.setdefault(pk, {}).update(a)
        h.db_delete = lambda pk: self.db.pop(pk, None)
        h.db_list = lambda kind, limit=200: [i for i in self.db.values() if i.get("gsi1pk") == kind]
        h.save_voicemail = lambda url, key: self.saved.append((url, key))
        h.telnyx = lambda ccid, action, body, event_id="": (
            self.cmds.append((ccid, action, body)), action not in self.fail_actions)[1]
        self.n = 0

    def ev(self, etype, ccid="A", state=None, **payload):
        self.n += 1
        p = {"call_control_id": ccid, "call_session_id": "S1", **payload}
        if state:
            p["client_state"] = state
        h.handle_webhook({"data": {"event_type": etype, "id": f"e{self.n}", "payload": p}})

    def last(self):
        return self.cmds[-1]

    def inbound(self, frm="+12155551234", to="+16095550001"):
        self.ev("call.initiated", direction="incoming", to=to, **{"from": frm})

    def allow(self, num, name="Mom"):
        self.db[f"NUM#{num}"] = {"pk": f"NUM#{num}", "status": "allow", "name": name}


class Flow(Base):
    def test_blocked_is_rejected(self):
        self.db["NUM#+12155551234"] = {"status": "block"}
        self.inbound()
        self.assertEqual(self.last()[1], "reject")
        self.assertEqual(self.db["CALL#S1"]["result"], "blocked")

    def test_allowed_rings_then_answers(self):
        self.allow("+12155551234")
        self.inbound(to="+16095550002")
        ccid, action, body = self.last()
        self.assertEqual((ccid, action), ("A", "transfer"))
        self.assertEqual(body["to"], "sip:me@sip.telnyx.com")
        self.assertEqual(body["from"], "+16095550002")  # our DID, not the caller's number
        self.assertEqual(body["from_display_name"], "GV Mom")
        ring = body["client_state"]
        self.ev("call.initiated", ccid="B", state=ring, direction="outgoing")
        self.ev("call.answered", ccid="B", state=ring)
        self.assertEqual(self.db["CALL#S1"]["result"], "answered")
        n = len(self.cmds)
        self.ev("call.hangup", ccid="B", state=ring, hangup_cause="normal_clearing")
        self.assertEqual(len(self.cmds), n)  # no voicemail after an answered call

    def test_allowed_no_answer_goes_to_voicemail(self):
        self.allow("+12155551234")
        self.inbound()
        ring = self.last()[2]["client_state"]
        self.ev("call.hangup", ccid="B", state=ring, hangup_cause="timeout")
        ccid, action, body = self.last()
        self.assertEqual((ccid, action), ("A", "answer"))  # leg A was still unanswered
        self.ev("call.answered", state=body["client_state"])
        self.assertEqual(self.last()[1], "speak")
        self.ev("call.speak.ended", state=self.last()[2]["client_state"])
        self.assertEqual(self.last()[1], "record_start")
        self.ev("call.recording.saved", recording_urls={"mp3": "https://x/rec.mp3"})
        self.assertEqual(self.saved, [("https://x/rec.mp3", "voicemail/S1.mp3")])
        self.assertEqual(self.db["CALL#S1"]["voicemail"], "voicemail/S1.mp3")

    def test_unknown_passes_challenge(self):
        self.inbound()
        ccid, action, body = self.last()
        self.assertEqual(action, "answer")
        self.ev("call.answered", state=body["client_state"])
        _, action, body = self.last()
        self.assertEqual(action, "gather_using_speak")
        self.assertEqual(body["valid_digits"], "1")
        self.ev("call.gather.ended", state=body["client_state"], digits="1", status="valid")
        ccid, action, body = self.last()
        self.assertEqual((ccid, action), ("A", "transfer"))
        self.assertEqual(body["from_display_name"], "Cell +12155551234")
        self.assertEqual(body["from"], "+16095550001")
        # softphone does not pick up: leg A is already answered, so greet directly
        self.ev("call.hangup", ccid="B", state=body["client_state"], hangup_cause="timeout")
        self.assertEqual(self.last()[:2], ("A", "speak"))

    def test_unknown_fails_challenge_gets_voicemail(self):
        self.inbound()
        self.ev("call.answered", state=self.last()[2]["client_state"])
        self.ev("call.gather.ended", state=self.last()[2]["client_state"], digits="", status="invalid")
        self.assertEqual(self.last()[1], "speak")
        self.assertEqual(self.db["CALL#S1"]["result"], "screen_failed")

    def test_hangup_during_challenge_sends_nothing(self):
        self.inbound()
        self.ev("call.answered", state=self.last()[2]["client_state"])
        n = len(self.cmds)
        self.ev("call.gather.ended", state=self.last()[2]["client_state"], digits="", status="call_hangup")
        self.assertEqual(len(self.cmds), n)

    def test_unreachable_softphone_falls_back_to_voicemail_after_challenge(self):
        """Transfer rejected outright (softphone not registered): no leg B, so no hangup webhook."""
        self.fail_actions.add("transfer")
        self.inbound()
        self.ev("call.answered", state=self.last()[2]["client_state"])
        self.ev("call.gather.ended", state=self.last()[2]["client_state"], digits="1", status="valid")
        self.assertEqual(self.last()[:2], ("A", "speak"))
        self.assertEqual(self.db["CALL#S1"]["result"], "ring_failed")

    def test_unreachable_softphone_falls_back_for_allowed_caller(self):
        self.fail_actions.add("transfer")
        self.allow("+12155551234")
        self.inbound()
        ccid, action, body = self.last()
        self.assertEqual((ccid, action), ("A", "answer"))
        self.ev("call.answered", state=body["client_state"])
        self.assertEqual(self.last()[1], "speak")
        self.assertEqual(self.db["CALL#S1"]["result"], "ring_failed")

    def test_anonymous_caller_is_challenged(self):
        self.inbound(frm="anonymous")
        self.assertEqual(self.last()[1], "answer")
        self.assertEqual(self.db["CALL#S1"]["from"], "anonymous")

    def test_normalize(self):
        self.assertEqual(h.normalize("(609) 254-2233"), "+16092542233")
        self.assertEqual(h.normalize("16092542233"), "+16092542233")
        self.assertEqual(h.normalize("+442071234567"), "+442071234567")
        self.assertEqual(h.normalize("Restricted"), "")

    def test_inbound_text_is_stored(self):
        h.handle_webhook({"data": {"event_type": "message.received", "id": "m1", "payload": {
            "id": "msg-1", "from": {"phone_number": "+12155551234"},
            "to": [{"phone_number": "+16095550002"}], "text": "hey", "media": []}}})
        msg = [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]
        self.assertEqual((msg["from"], msg["line"], msg["text"]), ("+12155551234", "GV", "hey"))

    def test_admin_requires_token_and_saves_numbers(self):
        def call(method, path, body="", token=None, query=None):
            return h.lambda_handler({
                "requestContext": {"http": {"method": method}}, "rawPath": path,
                "headers": {"x-admin-token": token} if token else {},
                "queryStringParameters": query, "body": body}, None)
        self.assertEqual(call("GET", "/admin/api/calls")["statusCode"], 401)
        self.assertEqual(call("GET", "/admin/api/calls", token="wrong")["statusCode"], 401)
        r = call("POST", "/admin/api/numbers", token="secret",
                 body='{"numbers":[{"number":"609-555-0123","status":"allow","name":"Dr S"},{"number":"junk","status":"allow"}]}')
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.db["NUM#+16095550123"]["status"], "allow")
        self.assertIn('"saved": 1', r["body"])
        self.assertEqual(call("GET", "/admin")["statusCode"], 200)

    def test_webhook_rejects_unsigned(self):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}}, "rawPath": "/telnyx",
                              "headers": {}, "body": "{}"}, None)
        self.assertEqual(r["statusCode"], 401)


if __name__ == "__main__":
    unittest.main()


class Texting(Base):
    def setUp(self):
        super().setUp()
        h.OFFICE_PHONE = "609-555-0100"
        self.sent = []
        h.GATEWAY_USER = h.GATEWAY_PASS = "x"

    def test_gateway_webhook_stores_cell_text(self):
        h.on_gateway_post({"id": "g1", "payload": {
            "phoneNumber": "+12155551234", "message": "running late"}})
        m = [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]
        self.assertEqual((m["contact"], m["line"], m["src"], m["dir"]),
                         ("+12155551234", "Cell", "cell", "in"))
        self.assertNotIn("medical", m)

    def test_simpapp_payload_shape_is_accepted(self):
        h.on_gateway_post({"type": "incoming_sms", "sender": "+12155551234",
                           "message": "running late"})
        m = [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]
        self.assertEqual((m["contact"], m["src"], m["text"]),
                         ("+12155551234", "cell", "running late"))

    def test_delivery_status_webhook_is_ignored(self):
        h.on_gateway_post({"type": "delivery_status", "id": "x", "status": "delivered"})
        self.assertEqual([i for i in self.db.values() if i.get("gsi1pk") == "MSGS"], [])

    def test_send_uses_bearer_api_when_key_is_set(self):
        h.store_message("c-1", "+12155551234", "", "Cell", "hi", src="cell")
        h.SMS_API_KEY = "KEY"
        calls, real = [], h.urllib.request.urlopen

        def fake(req, timeout=0):
            calls.append(req)
            return type("R", (), {"read": lambda s: b"{}", "__enter__": lambda s: s,
                                  "__exit__": lambda s, *a: None})()
        h.urllib.request.urlopen = fake
        try:
            ok, note = h.send_text("+12155551234", "ok")
        finally:
            h.urllib.request.urlopen = real
            h.SMS_API_KEY = ""
        self.assertTrue(ok, note)
        self.assertEqual(json.loads(calls[0].data),
                         {"phoneNumber": "+12155551234", "message": "ok"})
        self.assertEqual(calls[0].headers["Authorization"], "Bearer KEY")

    def test_medical_text_is_flagged_and_reply_offered(self):
        h.on_gateway_post({"id": "g2", "payload": {
            "phoneNumber": "+12155551234", "message": "my chest hurts, can I get a refill"}})
        m = [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]
        self.assertTrue(m["medical"])
        self.assertIn("609-555-0100", h.office_reply())
        self.assertIn("911", h.office_reply())

    def test_ordinary_text_is_not_flagged(self):
        for body in ["dinner at 7?", "the dog is out back", "happy birthday!"]:
            self.db.clear()
            h.on_gateway_post({"id": "g", "payload": {"phoneNumber": "+1215555" + "1234", "message": body}})
            m = [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]
            self.assertNotIn("medical", m, body)

    def test_reply_routes_to_google_voice_when_thread_came_from_there(self):
        h.store_message("gv-1", "+12155551234", "", "GV", "hi", src="gv",
                        relay="abc@txt.voice.google.com")
        h.GMAIL_USER, h.GMAIL_APP_PASSWORD = "me@gmail.com", "pw"
        import types, sys
        fake = types.ModuleType("gvmail")
        fake.send = lambda u, p, relay, text: self.sent.append((relay, text))
        sys.modules["gvmail"] = fake
        ok, note = h.send_text("+12155551234", "on my way")
        self.assertTrue(ok, note)
        self.assertEqual(self.sent, [("abc@txt.voice.google.com", "on my way")])
        out = [i for i in self.db.values() if i.get("dir") == "out"][0]
        self.assertEqual((out["src"], out["line"]), ("gv", "GV"))

    def test_reply_routes_to_cell_gateway_otherwise(self):
        h.store_message("c-1", "+12155551234", "", "Cell", "hi", src="cell")
        calls, real = [], h.urllib.request.urlopen

        def fake(req, timeout=0):
            calls.append(req)
            return type("R", (), {"read": lambda s: b"{}", "__enter__": lambda s: s,
                                  "__exit__": lambda s, *a: None})()
        h.urllib.request.urlopen = fake
        try:
            ok, note = h.send_text("+12155551234", "ok")
        finally:
            h.urllib.request.urlopen = real
        self.assertTrue(ok, note)
        self.assertEqual(json.loads(calls[0].data)["phoneNumbers"], ["+12155551234"])

    def test_gateway_endpoint_rejects_bad_secret(self):
        h.GATEWAY_SECRET = "right"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/gateway", "headers": {"x-gateway-secret": "wrong"},
                              "body": "{}"}, None)
        self.assertEqual(r["statusCode"], 401)


class Transcription(Base):
    def setUp(self):
        super().setUp()
        self.queued = []
        self.audio = {}
        h.save_voicemail = lambda url, key: self.audio.__setitem__(key, b"AUDIO")
        h.read_audio = lambda key: self.audio[key]
        h.transcribe_later = lambda pk, key, field: self.queued.append((pk, key, field))
        h.transcribe = lambda audio, filename="a.mp3": "hello this is a test"

    def answered_call(self):
        self.db["NUM#+12155551234"] = {"status": "allow", "name": "Mom"}
        self.inbound()
        ring = self.last()[2]["client_state"]
        self.ev("call.answered", ccid="B", state=ring)
        return ring

    def test_answered_call_is_not_recorded_by_default(self):
        h.RECORD_CALLS = False
        self.answered_call()
        self.assertNotIn("record_start", [c[1] for c in self.cmds])

    def test_answered_call_is_recorded_when_switched_on(self):
        h.RECORD_CALLS = True
        try:
            self.answered_call()
        finally:
            h.RECORD_CALLS = False
        ccid, action, body = self.last()
        self.assertEqual((ccid, action), ("A", "record_start"))
        self.assertEqual(self.db["CALL#S1"]["recording_kind"], "call")

    def test_conversation_recording_is_stored_and_queued(self):
        self.db["CALL#S1"] = {"pk": "CALL#S1", "recording_kind": "call"}
        self.ev("call.recording.saved", recording_urls={"mp3": "https://x/r.mp3"})
        self.assertEqual(self.db["CALL#S1"]["recording"], "calls/S1.mp3")
        self.assertEqual(self.queued, [("CALL#S1", "calls/S1.mp3", "transcript")])

    def test_voicemail_recording_is_stored_and_queued_separately(self):
        self.db["CALL#S1"] = {"pk": "CALL#S1", "recording_kind": "voicemail"}
        self.ev("call.recording.saved", recording_urls={"mp3": "https://x/v.mp3"})
        self.assertEqual(self.db["CALL#S1"]["voicemail"], "voicemail/S1.mp3")
        self.assertEqual(self.queued, [("CALL#S1", "voicemail/S1.mp3", "vm_transcript")])

    def test_transcription_job_writes_the_text(self):
        self.audio["voicemail/S1.mp3"] = b"AUDIO"
        self.db["CALL#S1"] = {"pk": "CALL#S1"}
        h.run_transcription({"pk": "CALL#S1", "key": "voicemail/S1.mp3", "field": "vm_transcript"})
        self.assertEqual(self.db["CALL#S1"]["vm_transcript"], "hello this is a test")

    def test_failed_transcription_leaves_a_marker_not_a_crash(self):
        h.transcribe = lambda audio, filename="a.mp3": ""
        self.audio["calls/S1.mp3"] = b"AUDIO"
        self.db["CALL#S1"] = {"pk": "CALL#S1"}
        h.run_transcription({"pk": "CALL#S1", "key": "calls/S1.mp3", "field": "transcript"})
        self.assertEqual(self.db["CALL#S1"]["transcript"], "(no transcript)")


class GatewayAuth(Base):
    def call(self, headers=None, query=None):
        h.GATEWAY_SECRET = "right"
        return h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                                 "rawPath": "/gateway", "headers": headers or {},
                                 "queryStringParameters": query, "body": "{}"}, None)

    def test_secret_in_header_is_accepted(self):
        self.assertEqual(self.call(headers={"x-gateway-secret": "right"})["statusCode"], 200)

    def test_secret_in_x_api_key_header_is_accepted(self):
        self.assertEqual(self.call(headers={"x-api-key": "right"})["statusCode"], 200)

    def test_secret_in_query_string_is_accepted(self):
        self.assertEqual(self.call(query={"k": "right"})["statusCode"], 200)

    def test_wrong_secret_is_rejected_either_way(self):
        self.assertEqual(self.call(query={"k": "wrong"})["statusCode"], 401)
        self.assertEqual(self.call(headers={"x-gateway-secret": "wrong"})["statusCode"], 401)
        self.assertEqual(self.call()["statusCode"], 401)


class Pages(Base):
    def get(self, path):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": path, "headers": {}, "body": ""}, None)

    def test_manifest_is_served_without_a_token(self):
        r = self.get("/manifest.webmanifest")
        self.assertEqual(r["statusCode"], 200)
        m = json.loads(r["body"])
        self.assertEqual((m["start_url"], m["display"]), ("/admin", "standalone"))
        self.assertIn("manifest", r["headers"]["content-type"])

    def test_admin_page_is_responsive_and_links_the_manifest(self):
        body = self.get("/admin")["body"]
        self.assertIn('rel="manifest"', body)
        self.assertIn("max-width: 720px", body)       # layout switch
        self.assertIn("min-width: 721px", body)       # side-by-side panes
        self.assertIn("viewport-fit=cover", body)     # safe-area on phones

    def test_hidden_panels_actually_hide(self):
        """Rules like .composer { display: flex } beat the UA's [hidden] rule."""
        body = self.get("/admin")["body"]
        self.assertIn("[hidden] { display: none !important; }", body)


class EitherEndpoint(Base):
    """Telnyx on /gateway or the phone app on /telnyx should both still work."""

    def post(self, path, body, signed=False, key=None):
        real = h.verify_signature
        h.verify_signature = lambda raw, headers: signed
        h.GATEWAY_SECRET = "s3cret"
        try:
            return h.lambda_handler({
                "requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                "headers": {}, "queryStringParameters": {"k": key} if key else None,
                "body": json.dumps(body)}, None)
        finally:
            h.verify_signature = real

    def telnyx_text(self):
        return {"data": {"event_type": "message.received", "id": "m1", "payload": {
            "id": "x1", "from": {"phone_number": "+12155551234"},
            "to": [{"phone_number": "+16095550002"}], "text": "from telnyx"}}}

    def app_text(self):
        return {"type": "incoming_sms", "sender": "+12155559999", "message": "from phone"}

    def stored(self):
        return [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"]

    def test_signed_telnyx_payload_on_gateway_path(self):
        self.assertEqual(self.post("/gateway", self.telnyx_text(), signed=True)["statusCode"], 200)
        self.assertEqual(self.stored()[0]["text"], "from telnyx")

    def test_app_payload_with_key_on_telnyx_path(self):
        self.assertEqual(self.post("/telnyx", self.app_text(), key="s3cret")["statusCode"], 200)
        self.assertEqual(self.stored()[0]["text"], "from phone")

    def test_each_still_works_on_its_own_path(self):
        self.post("/telnyx", self.telnyx_text(), signed=True)
        self.post("/gateway", self.app_text(), key="s3cret")
        self.assertEqual(sorted(m["text"] for m in self.stored()),
                         ["from phone", "from telnyx"])

    def test_unauthenticated_is_still_rejected_on_both(self):
        for path in ("/telnyx", "/gateway"):
            self.assertEqual(self.post(path, self.app_text())["statusCode"], 401)
            self.assertEqual(self.post(path, self.app_text(), key="wrong")["statusCode"], 401)


class Status(Base):
    def status(self):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "rawPath": "/admin/api/status",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None, "body": ""}, None)
        return json.loads(r["body"])

    def test_reports_a_poll_that_has_never_run(self):
        self.assertIn("not run yet", self.status()["gmail"]["note"])

    def test_password_length_is_reported_but_not_the_password(self):
        h.GMAIL_APP_PASSWORD = "abcdefghijklmnop"
        try:
            out = self.status()
        finally:
            h.GMAIL_APP_PASSWORD = ""
        self.assertEqual(out["gmail_password_len"], 16)
        self.assertNotIn("abcdefghijklmnop", json.dumps(out))

    def test_missing_credentials_are_recorded_not_silently_ignored(self):
        h.GMAIL_USER = h.GMAIL_APP_PASSWORD = ""
        out = h.poll_gmail()
        self.assertIn("no Gmail user", out["error"])
        self.assertIn("no Gmail user", self.status()["gmail"]["error"])

    def test_a_failing_fetch_is_recorded(self):
        h.GMAIL_USER, h.GMAIL_APP_PASSWORD = "me@gmail.com", "pw"
        import sys, types
        fake = types.ModuleType("gvmail")

        def boom(*a, **k):
            raise OSError("authentication failed")
        fake.fetch = boom
        sys.modules["gvmail"] = fake
        try:
            out = h.poll_gmail()
        finally:
            sys.modules.pop("gvmail", None)
            h.GMAIL_USER = h.GMAIL_APP_PASSWORD = ""
        self.assertIn("authentication failed", out["error"])
        self.assertIn("authentication failed", self.status()["gmail"]["error"])


class Credentials(Base):
    def test_app_password_spaces_are_stripped(self):
        """Google displays app passwords as 'abcd efgh ijkl mnop'."""
        import importlib
        os.environ["GMAIL_APP_PASSWORD"] = "abcd efgh ijkl mnop"
        os.environ["GMAIL_USER"] = " me@gmail.com "
        try:
            importlib.reload(h)
            self.assertEqual(h.GMAIL_APP_PASSWORD, "abcdefghijklmnop")
            self.assertEqual(h.GMAIL_USER, "me@gmail.com")
        finally:
            os.environ.pop("GMAIL_APP_PASSWORD"); os.environ.pop("GMAIL_USER")
            importlib.reload(h)


class NameOnlyStatus(Base):
    def save(self, rows):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/numbers",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"numbers": rows})}, None)
        return json.loads(r["body"])

    def test_name_only_contact_is_stored(self):
        out = self.save([{"number": "609-555-0123", "name": "Dr Smith", "status": "name"}])
        self.assertEqual(out["saved"], 1)
        self.assertEqual(self.db["NUM#+16095550123"]["status"], "name")

    def test_name_only_caller_is_still_screened_but_labelled(self):
        self.save([{"number": "+12155551234", "name": "Dr Smith", "status": "name"}])
        self.inbound()
        self.assertEqual(self.last()[1], "answer")        # challenged, not rung through
        rec = self.db["CALL#S1"]
        self.assertEqual((rec["name"], rec["list"]), ("Dr Smith", "name"))

    def test_rubbish_status_is_still_refused(self):
        out = self.save([{"number": "609-555-0123", "status": "vip"}])
        self.assertEqual(out["saved"], 0)


class NewConversation(Base):
    def setUp(self):
        super().setUp()
        h.SMS_API_KEY = "KEY"
        self.posted, self.real = [], h.urllib.request.urlopen

        def fake(req, timeout=0):
            self.posted.append(req)
            return type("R", (), {"read": lambda s: b"{}", "__enter__": lambda s: s,
                                  "__exit__": lambda s, *a: None})()
        h.urllib.request.urlopen = fake

    def tearDown(self):
        h.urllib.request.urlopen = self.real
        h.SMS_API_KEY = ""

    def test_number_with_no_history_sends_from_the_cell(self):
        ok, note = h.send_text("+12155557777", "hello", via="cell")
        self.assertTrue(ok, note)
        self.assertEqual(json.loads(self.posted[0].data)["phoneNumber"], "+12155557777")

    def test_via_cell_overrides_an_existing_google_voice_thread(self):
        h.store_message("gv-1", "+12155551234", "", "GV", "hi", src="gv",
                        relay="r@txt.voice.google.com")
        ok, note = h.send_text("+12155551234", "hello", via="cell")
        self.assertTrue(ok, note)
        self.assertIn("cell", note)

    def test_google_voice_without_a_relay_is_refused_clearly(self):
        h.GMAIL_USER, h.GMAIL_APP_PASSWORD = "me@gmail.com", "pw"
        try:
            ok, note = h.send_text("+12155558888", "hello", via="gv")
        finally:
            h.GMAIL_USER = h.GMAIL_APP_PASSWORD = ""
        self.assertFalse(ok)
        self.assertIn("relay", note)


class ThreadActions(Base):
    def post(self, contact, action):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/thread",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"contact": contact, "action": action})}, None)
        return r["statusCode"], json.loads(r["body"])

    def populate(self):
        self.inbound()                       # a call from +12155551234
        h.store_message("m1", "+12155551234", "", "Cell", "hi", src="cell")
        h.store_message("m2", "+19995550000", "", "Cell", "other", src="cell")

    def test_archive_marks_only_that_thread(self):
        self.populate()
        code, out = self.post("+12155551234", "archive")
        self.assertEqual(code, 200)
        self.assertEqual(out["archived"], 2)
        self.assertTrue(self.db["CALL#S1"]["archived"])
        self.assertTrue(self.db["MSG#m1"]["archived"])
        self.assertNotIn("archived", self.db["MSG#m2"])

    def test_unarchive_restores(self):
        self.populate()
        self.post("+12155551234", "archive")
        code, out = self.post("+12155551234", "unarchive")
        self.assertEqual(out["restored"], 2)
        self.assertFalse(self.db["MSG#m1"]["archived"])

    def test_delete_removes_only_that_thread(self):
        self.populate()
        code, out = self.post("+12155551234", "delete")
        self.assertEqual(out["deleted"], 2)
        self.assertNotIn("CALL#S1", self.db)
        self.assertNotIn("MSG#m1", self.db)
        self.assertIn("MSG#m2", self.db)

    def test_bad_action_is_refused(self):
        self.assertEqual(self.post("+12155551234", "burn")[0], 400)


class DeleteOddNumbers(Base):
    def delete(self, number):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "DELETE"}},
                              "rawPath": "/admin/api/numbers",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": {"number": number},
                              "body": ""}, None)
        return r["statusCode"]

    def test_entry_that_does_not_normalise_can_still_be_removed(self):
        """A short code or extension never normalises, so it was undeletable."""
        self.db["NUM#2200"] = {"pk": "NUM#2200", "number": "2200", "status": "allow"}
        self.assertEqual(self.delete("2200"), 200)
        self.assertNotIn("NUM#2200", self.db)

    def test_normal_number_still_deletes(self):
        self.db["NUM#+16095550123"] = {"pk": "NUM#+16095550123", "status": "allow"}
        self.assertEqual(self.delete("609-555-0123"), 200)
        self.assertNotIn("NUM#+16095550123", self.db)


class ShortCodeThreads(Base):
    def post(self, contact, action):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/thread",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"contact": contact, "action": action})}, None)
        return r["statusCode"], json.loads(r["body"])

    def test_short_code_thread_can_be_deleted(self):
        h.store_message("sc1", "22000", "", "Cell", "your code is 1234", src="telnyx")
        code, out = self.post("22000", "delete")
        self.assertEqual(code, 200)
        self.assertEqual(out["deleted"], 1)
        self.assertNotIn("MSG#sc1", self.db)

    def test_short_code_thread_can_be_archived(self):
        h.store_message("sc2", "22000", "", "Cell", "code", src="telnyx")
        code, out = self.post("22000", "archive")
        self.assertEqual((code, out["archived"]), (200, 1))
        self.assertTrue(self.db["MSG#sc2"]["archived"])

    def test_archive_and_delete_buttons_are_not_gated_on_a_plus_prefix(self):
        page = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""}, None)["body"]
        after = page.split("Allow</button>")[-1]
        self.assertIn("'delete'", after)


class RecordingSwitch(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.RECORD_CALLS = False

    def set(self, on):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/settings",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"record": on})}, None)
        return r["statusCode"], json.loads(r["body"])

    def answer_a_call(self):
        self.db["NUM#+12155551234"] = {"status": "allow", "name": "Mom"}
        self.inbound()
        ring = self.last()[2]["client_state"]
        self.ev("call.answered", ccid="B", state=ring)

    def test_defaults_to_the_deployed_value(self):
        self.assertFalse(h.recording_on())

    def test_toggling_on_takes_effect_without_a_deploy(self):
        self.assertEqual(self.set(True), (200, {"record_calls": True}))
        self.assertTrue(h.recording_on())
        self.answer_a_call()
        self.assertEqual(self.last()[:2], ("A", "record_start"))

    def test_toggling_back_off_stops_recording(self):
        self.set(True)
        self.set(False)
        self.assertFalse(h.recording_on())
        self.answer_a_call()
        self.assertNotIn("record_start", [c[1] for c in self.cmds])

    def test_stored_switch_overrides_a_deployed_on(self):
        h.RECORD_CALLS = True
        try:
            self.set(False)
            self.assertFalse(h.recording_on())
        finally:
            h.RECORD_CALLS = False

    def test_empty_body_is_refused(self):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/settings",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None, "body": "{}"}, None)
        self.assertEqual(r["statusCode"], 400)


class OutboundCalls(Base):
    def outbound(self, to="+12155551234"):
        self.ev("call.initiated", direction="outgoing", to=to, **{"from": "+16098205401"})

    def test_call_placed_from_the_softphone_is_logged(self):
        self.db["NUM#+12155551234"] = {"status": "allow", "name": "Mom"}
        self.outbound()
        rec = self.db["CALL#S1"]
        self.assertEqual((rec["from"], rec["dir"], rec["line"], rec["name"], rec["result"]),
                         ("+12155551234", "out", "Outbound", "Mom", "dialed"))

    def test_outbound_answer_and_hangup_update_it(self):
        self.outbound()
        self.ev("call.answered")
        self.assertEqual(self.db["CALL#S1"]["result"], "answered")
        self.ev("call.hangup", hangup_cause="normal_clearing")
        self.assertEqual(self.db["CALL#S1"]["result"], "answered")

    def test_unanswered_outbound_is_marked(self):
        self.outbound()
        self.ev("call.hangup", hangup_cause="no_answer")
        self.assertEqual(self.db["CALL#S1"]["result"], "no answer")

    def test_the_leg_we_create_to_ring_the_softphone_is_not_logged_twice(self):
        self.db["NUM#+12155551234"] = {"status": "allow", "name": "Mom"}
        self.inbound()                                  # inbound call, logged once
        ring = self.last()[2]["client_state"]
        self.ev("call.initiated", ccid="B", state=ring, direction="outgoing",
                to="sip:me@sip.telnyx.com")
        self.assertEqual(self.db["CALL#S1"]["line"], "Cell")   # not overwritten
        self.assertNotEqual(self.db["CALL#S1"].get("dir"), "out")


class ClickToCall(Base):
    def setUp(self):
        super().setUp()
        h.API_KEY, h.RING_TARGET = "k", "sip:me@sip.telnyx.com"
        os.environ["VOICE_APP_ID"] = "app-1"
        self.posted, self.real = [], h.urllib.request.urlopen

        def fake(req, timeout=0):
            self.posted.append(req)
            return type("R", (), {"read": lambda s: b"{}", "__enter__": lambda s: s,
                                  "__exit__": lambda s, *a: None})()
        h.urllib.request.urlopen = fake

    def tearDown(self):
        h.urllib.request.urlopen = self.real
        os.environ.pop("VOICE_APP_ID", None)

    def call(self, to):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/call",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"to": to})}, None)
        return r["statusCode"], json.loads(r["body"])

    def test_rings_the_handset_first(self):
        code, out = self.call("215-555-1234")
        self.assertEqual(code, 200)
        self.assertTrue(out["ok"])
        sent = json.loads(self.posted[0].data)
        self.assertEqual(sent["to"], "sip:me@sip.telnyx.com")
        self.assertEqual(h.decode_state(sent["client_state"]),
                         {"s": "ctc", "t": "+12155551234"})

    def test_answering_bridges_to_the_person(self):
        self.call("215-555-1234")
        state = json.loads(self.posted[0].data)["client_state"]
        self.ev("call.answered", state=state)
        ccid, action, body = self.last()
        self.assertEqual(action, "transfer")
        self.assertEqual(body["to"], "+12155551234")

    def test_bad_number_is_refused(self):
        self.assertEqual(self.call("nonsense")[0], 400)

    def test_missing_voice_app_id_says_which_setting_is_missing(self):
        os.environ.pop("VOICE_APP_ID", None)
        code, out = self.call("215-555-1234")
        self.assertEqual(code, 502)
        self.assertIn("VOICE_APP_ID", out["note"])
        self.assertEqual(self.posted, [])   # nothing dialled


class TextSize(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""}, None)["body"]

    def test_base_font_comes_from_a_variable(self):
        body = self.page()
        self.assertIn("--fs: 16px", body)
        self.assertIn("font: var(--fs)/1.45", body)

    def test_size_is_clamped_and_remembered(self):
        body = self.page()
        self.assertIn("FS_MIN = 13, FS_MAX = 26", body)
        self.assertIn("localStorage.setItem('fontSize', fs)", body)

    def test_no_text_is_left_pinned_to_pixels(self):
        """Pinned px font sizes would not scale with the control."""
        import re
        css = self.page().split("</style>")[0]
        pinned = [m for m in re.findall(r"font-size:\s*([^;]+);", css)
                  if "px" in m and "--fs" not in m and "max(" not in m]
        self.assertEqual(pinned, [], f"still pinned: {pinned}")

    def test_inputs_stay_at_least_16px_so_phones_do_not_zoom(self):
        self.assertIn("font-size: max(16px, 1em)", self.page())


class GoogleContacts(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.GOOGLE_CLIENT_ID, h.GOOGLE_CLIENT_SECRET = "cid", "csec"
        import sys, types
        self.fake = types.ModuleType("gcontacts")
        self.fake.consent_url = lambda cid, uri, state: f"https://consent?state={state}"
        self.fake.exchange_code = lambda *a: {"refresh_token": "RT"}
        self.fake.access_token = lambda *a: "AT"
        self.fake.connections = lambda token, page_limit=20: self.pairs
        self.fake.other_contacts = lambda token, page_limit=20: self.other
        self.other = []
        self.pairs = [("+1 215 555 1234", "Mom"), ("609-555-0123", "Dr Smith")]
        sys.modules["gcontacts"] = self.fake

    def tearDown(self):
        import sys
        sys.modules.pop("gcontacts", None)
        h.GOOGLE_CLIENT_ID = h.GOOGLE_CLIENT_SECRET = ""

    def req(self, method, path, headers=None, query=None, body=""):
        return h.lambda_handler({"requestContext": {"http": {"method": method}},
                                 "rawPath": path,
                                 "headers": headers or {"host": "api.example.com"},
                                 "queryStringParameters": query, "body": body}, None)

    def start(self):
        r = self.req("GET", "/admin/api/oauth-url",
                     headers={"x-admin-token": "secret", "host": "api.example.com"})
        return json.loads(r["body"])

    def test_consent_url_uses_a_one_time_state(self):
        out = self.start()
        self.assertEqual(out["redirect_uri"], "https://api.example.com/oauth/callback")
        state = out["url"].split("state=")[1]
        self.assertIn(f"OAUTH#{state}", self.db)

    def test_callback_stores_the_token_and_imports_names(self):
        state = self.start()["url"].split("state=")[1]
        r = self.req("GET", "/oauth/callback", query={"state": state, "code": "c"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(self.db["STATE#gtoken"]["refresh_token"], "RT")
        self.assertEqual(self.db["NUM#+12155551234"]["name"], "Mom")
        self.assertEqual(self.db["NUM#+16095550123"]["status"], "name")
        self.assertNotIn(f"OAUTH#{state}", self.db)   # state is single use

    def test_callback_with_an_unknown_state_is_refused(self):
        r = self.req("GET", "/oauth/callback", query={"state": "forged", "code": "c"})
        self.assertEqual(r["statusCode"], 400)
        self.assertNotIn("STATE#gtoken", self.db)

    def test_sync_never_overwrites_an_allow_or_block_choice(self):
        self.db["NUM#+12155551234"] = {"pk": "NUM#+12155551234", "status": "block",
                                       "name": "old"}
        self.db["STATE#gtoken"] = {"refresh_token": "RT"}
        h.sync_contacts()
        entry = self.db["NUM#+12155551234"]
        self.assertEqual(entry["status"], "block")     # decision kept
        self.assertEqual(entry["name"], "Mom")         # name refreshed

    def test_contacts_without_a_name_or_a_usable_number_are_skipped(self):
        self.pairs = [("+12155551234", ""), ("x", "No Number"), ("+16095550123", "Ok")]
        self.db["STATE#gtoken"] = {"refresh_token": "RT"}
        out = h.sync_contacts()
        self.assertEqual((out["written"], out["skipped"]), (1, 2))

    def test_auto_collected_contacts_are_merged_in(self):
        self.other = [("+19175550000", "Taxi")]
        self.db["STATE#gtoken"] = {"refresh_token": "RT"}
        out = h.sync_contacts()
        self.assertEqual((out["saved"], out["other"]), (2, 1))
        self.assertEqual(self.db["NUM#+19175550000"]["name"], "Taxi")

    def test_sync_without_a_token_says_so_rather_than_failing_silently(self):
        out = h.sync_contacts()
        self.assertIn("not connected", out["error"])

    def test_scheduled_event_runs_a_sync(self):
        self.db["STATE#gtoken"] = {"refresh_token": "RT"}
        out = h.lambda_handler({"contacts": True}, None)
        self.assertEqual(out["written"], 2)


class BatchActions(Base):
    def post(self, payload):
        h.ADMIN_TOKEN = "secret"
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/thread",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps(payload)}, None)
        return r["statusCode"], json.loads(r["body"])

    def populate(self):
        for i, num in enumerate(["+12155551234", "+19995550000", "22000"], 1):
            h.store_message(f"m{i}", num, "", "Cell", "hi", src="cell")
        h.store_message("m4", "+15555555555", "", "Cell", "keep me", src="cell")

    def test_archive_several_threads_at_once(self):
        self.populate()
        code, out = self.post({"contacts": ["+12155551234", "+19995550000", "22000"],
                               "action": "archive"})
        self.assertEqual((code, out["archived"], out["threads"]), (200, 3, 3))
        self.assertTrue(self.db["MSG#m1"]["archived"])
        self.assertNotIn("archived", self.db["MSG#m4"])

    def test_delete_several_threads_at_once(self):
        self.populate()
        code, out = self.post({"contacts": ["+12155551234", "22000"], "action": "delete"})
        self.assertEqual((code, out["deleted"]), (200, 2))
        self.assertNotIn("MSG#m1", self.db)
        self.assertIn("MSG#m2", self.db)

    def test_block_several_and_keep_their_names(self):
        self.db["NUM#+12155551234"] = {"pk": "NUM#+12155551234", "status": "allow",
                                       "name": "Spam Co"}
        code, out = self.post({"contacts": ["+12155551234", "609-555-0123"],
                               "action": "block"})
        self.assertEqual(code, 200)
        self.assertEqual(self.db["NUM#+12155551234"]["status"], "block")
        self.assertEqual(self.db["NUM#+12155551234"]["name"], "Spam Co")
        self.assertEqual(self.db["NUM#+16095550123"]["status"], "block")

    def test_single_contact_form_still_works(self):
        self.populate()
        code, out = self.post({"contact": "+12155551234", "action": "delete"})
        self.assertEqual((code, out["deleted"]), (200, 1))

    def test_empty_selection_is_refused(self):
        self.assertEqual(self.post({"contacts": [], "action": "delete"})[0], 400)

    def test_unknown_action_is_refused(self):
        self.assertEqual(self.post({"contacts": ["+12155551234"], "action": "nuke"})[0], 400)


class ButtonIcons(Base):
    def test_every_action_button_carries_a_glyph_and_its_words(self):
        page = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]
        # A nav tile puts its glyph in its own element; a button keeps it
        # inline. Either way the glyph and the word must stay together.
        flat = re.sub(r"\s+", " ", page.replace("<b>", "").replace("</b>", "")
                      .replace("&#65039;", ""))
        for glyph, word in [("&#9742;", "Call"), ("&#10003;", "Allow"),
                            ("&#8856;", "Block"), ("&#128465;", "Delete"),
                            ("&#9654;", "Voicemail"), ("&#10148;", "Send"),
                            ("&#128222;", "Comms"), ("&#9993;", "Email"),
                            ("&#9881;", "Status")]:
            self.assertTrue(f"{glyph} {word}" in flat or f"{glyph}{word}" in flat,
                            f"{word} lost its icon")


class MessageOrder(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_texts_are_newest_first_like_the_calls(self):
        page = self.page()
        self.assertIn("g.msgs.sort((a, b) => b.ts.localeCompare(a.ts))", page)
        self.assertIn("g.calls.sort((a, b) => b.ts.localeCompare(a.ts))", page)

    def test_preview_follows_the_newest_message(self):
        self.assertIn("const lastMsg = g.msgs[0];", self.page())

    def test_the_two_sides_are_different_colours(self):
        css = self.page().split("</style>")[0]
        self.assertIn("--them: #0f766e", css)
        self.assertIn("background: var(--them)", css)
        self.assertIn(".out .bubble { background: var(--accent)", css)


class ComposerPosition(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_mobile_composer_sits_above_the_thread(self):
        body = self.page()
        self.assertLess(body.index('id="composer"'), body.index('<main id="main"'))
        self.assertIn("border-bottom: 1px solid var(--line); flex-shrink: 0;",
                      body.split(".composer {")[1][:200])

    def test_desktop_reply_box_comes_before_the_messages(self):
        pane = self.page().split('<div class="pane"><h3>Texts</h3>')[1]
        self.assertLess(pane.index('class="reply"'), pane.index("textsHtml(g)"))


class Tasks(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.GOOGLE_CLIENT_ID, h.GOOGLE_CLIENT_SECRET = "cid", "csec"
        self.db["STATE#gtoken"] = {"pk": "STATE#gtoken", "refresh_token": "RT"}
        self.created = []
        import sys, types
        fake = types.ModuleType("gcontacts")
        fake.access_token = lambda *a: "AT"
        fake.task_lists = lambda token: self.lists
        fake.create_task = lambda token, lid, title, notes="": \
            self.created.append((lid, title, notes))
        self.lists = [("L1", "My Tasks"), ("L2", "Callbacks")]
        sys.modules["gcontacts"] = fake

    def tearDown(self):
        import sys
        sys.modules.pop("gcontacts", None)
        h.GOOGLE_CLIENT_ID = h.GOOGLE_CLIENT_SECRET = ""

    def post(self, path, payload):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": path,
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps(payload)}, None)
        return r["statusCode"], json.loads(r["body"])

    def test_task_lands_on_the_first_list_by_default(self):
        code, out = self.post("/admin/api/task",
                              {"contact": "+12155551234", "name": "Mom",
                               "text": "my chest hurts"})
        self.assertEqual((code, out["ok"]), (200, True))
        lid, title, notes = self.created[0]
        self.assertEqual(lid, "L1")
        self.assertEqual(title, "Call back Mom")
        self.assertIn("my chest hurts", notes)
        self.assertIn("+12155551234", notes)

    def test_chosen_list_is_used(self):
        self.post("/admin/api/settings", {"tasklist": "L2"})
        self.post("/admin/api/task", {"contact": "+12155551234", "text": "hi"})
        self.assertEqual(self.created[0][0], "L2")

    def test_contact_is_required(self):
        self.assertEqual(self.post("/admin/api/task", {"text": "hi"})[0], 400)

    def test_without_google_it_says_so(self):
        del self.db["STATE#gtoken"]
        code, out = self.post("/admin/api/task", {"contact": "+12155551234"})
        self.assertEqual(code, 502)
        self.assertIn("not connected", out["note"])

    def test_a_failure_is_raised_to_the_user_not_swallowed(self):
        page = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]
        fn = page.split("async function makeTask")[1].split("async function")[0]
        self.assertIn("alert(", fn)

    def test_short_code_thread_can_make_a_task(self):
        code, out = self.post("/admin/api/task", {"contact": "22000", "text": "code"})
        self.assertEqual(code, 200)
        self.assertEqual(self.created[0][1], "Call back 22000")


class ContactsLink(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_thread_links_to_a_contacts_search(self):
        body = self.page()
        self.assertIn("https://contacts.google.com/search/", body)
        self.assertIn("contactsLink(g)", body)

    def test_numbers_screen_links_to_contacts(self):
        self.assertIn('href="https://contacts.google.com/"', self.page())

    def test_links_open_safely_in_a_new_tab(self):
        """Every anchor out to Google, not the URL used inside the helper."""
        import re
        anchors = re.findall(r"<a\b[^>]*>", self.page())
        out = [a for a in anchors if "contacts.google.com" in a or "contactsLink" in a]
        self.assertTrue(out)
        for a in out:
            self.assertIn('target="_blank"', a)
            self.assertIn('rel="noopener"', a)


class MedTaskHandoff(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.MEDTASK_URL = "https://medtask.example/telnyx/comms-task"
        h.MEDTASK_SECRET = "sh4red"
        self.sent, self.real = [], h.urllib.request.urlopen
        self.status, self.reply = 201, {"success": True,
                                        "data": {"taskId": "T1", "dup": False}}

        def fake(req, timeout=0):
            self.sent.append(req)
            body = json.dumps(self.reply).encode()
            if self.status >= 400:
                raise h.urllib.error.HTTPError(
                    req.full_url, self.status, "err", {}, io.BytesIO(body))
            return type("R", (), {"status": self.status, "read": lambda s: body,
                                  "__enter__": lambda s: s,
                                  "__exit__": lambda s, *a: None})()
        h.urllib.request.urlopen = fake

    def tearDown(self):
        h.urllib.request.urlopen = self.real
        h.MEDTASK_URL = h.MEDTASK_SECRET = ""

    def post(self, payload):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/medtask",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps(payload)}, None)
        return r["statusCode"], json.loads(r["body"])

    def a_text(self):
        h.store_message("t1", "+12155551234", "", "Cell", "my chest hurts",
                        src="cell", name="Mary Smith")

    def test_text_is_sent_signed_and_as_one_message(self):
        self.a_text()
        code, out = self.post({"kind": "text", "id": "t1"})
        self.assertEqual((code, out["ok"]), (200, True))
        req = self.sent[0]
        body = json.loads(req.data)
        self.assertEqual(body["kind"], "text")
        self.assertEqual(body["phone"], "+12155551234")
        self.assertEqual(body["text"], "my chest hurts")
        self.assertEqual(body["name"], "Mary Smith")
        ts = req.headers["X-comms-timestamp"]
        want = hmac.new(b"sh4red", ts.encode() + b"." + req.data,
                        hashlib.sha256).hexdigest()
        self.assertEqual(req.headers["X-comms-signature"], f"sha256={want}")

    def test_id_is_stable_so_a_retry_does_not_make_a_second_task(self):
        self.a_text()
        self.post({"kind": "text", "id": "t1"})
        self.post({"kind": "text", "id": "t1"})
        self.assertEqual(json.loads(self.sent[0].data)["id"],
                         json.loads(self.sent[1].data)["id"])

    def test_duplicate_is_reported_as_already_sent(self):
        self.a_text()
        self.status, self.reply = 200, {"data": {"taskId": "T1", "dup": True}}
        code, out = self.post({"kind": "text", "id": "t1"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["dup"])

    def test_voicemail_sends_its_transcript(self):
        self.db["CALL#S9"] = {"pk": "CALL#S9", "from": "+12155551234",
                              "ts": h.now_iso(), "voicemail": "voicemail/S9.mp3",
                              "vm_transcript": "please call me back"}
        self.post({"kind": "call", "id": "S9"})
        body = json.loads(self.sent[0].data)
        self.assertEqual(body["kind"], "voicemail")
        self.assertEqual(body["text"], "please call me back")

    def test_a_call_with_no_transcript_sends_none(self):
        self.db["CALL#S8"] = {"pk": "CALL#S8", "from": "+12155551234",
                              "ts": h.now_iso(), "vm_transcript": "(transcribing…)"}
        self.post({"kind": "call", "id": "S8"})
        body = json.loads(self.sent[0].data)
        self.assertEqual(body["kind"], "call")
        self.assertNotIn("text", body)

    def test_thread_history_is_never_included(self):
        self.a_text()
        h.store_message("t2", "+12155551234", "", "Cell", "and another thing",
                        src="cell")
        self.post({"kind": "text", "id": "t1"})
        body = json.loads(self.sent[0].data)
        self.assertEqual(body["text"], "my chest hurts")
        self.assertNotIn("and another thing", json.dumps(body))

    def test_a_rejected_signature_is_reported_plainly(self):
        self.a_text()
        self.status, self.reply = 401, {"error": "bad signature"}
        code, out = self.post({"kind": "text", "id": "t1"})
        self.assertEqual(code, 502)
        self.assertIn("signature", out["note"])

    def test_switched_off_in_medtask_is_reported(self):
        self.a_text()
        self.status, self.reply = 503, {"error": "disabled"}
        code, out = self.post({"kind": "text", "id": "t1"})
        self.assertIn("Switched off", out["note"])

    def test_unconfigured_says_so_without_calling_out(self):
        h.MEDTASK_URL = ""
        self.a_text()
        code, out = self.post({"kind": "text", "id": "t1"})
        self.assertFalse(out["ok"])
        self.assertEqual(self.sent, [])

    def test_bad_kind_is_refused(self):
        self.assertEqual(self.post({"kind": "fax", "id": "t1"})[0], 400)


class NameResolution(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_panel_reads_names_from_the_number_list_not_the_message(self):
        page = self.page()
        self.assertIn("api('GET', 'numbers')", page.split("async function refresh")[1][:600])
        self.assertIn("g.name = names[key] || g.name", page)

    def test_contacts_search_uses_the_format_google_stores(self):
        fn = self.page().split("function contactsLink")[1].split("async function")[0]
        self.assertIn(r"^\+1(\d{3})(\d{3})(\d{4})$", fn)
        self.assertIn("`(${m[1]})${m[2]}-${m[3]}`", fn)

    def test_numbers_api_returns_what_the_lookup_needs(self):
        h.ADMIN_TOKEN = "secret"
        self.db["NUM#+16095550123"] = {"pk": "NUM#+16095550123", "gsi1pk": "NUMS",
                                       "number": "+16095550123", "name": "Dr Smith",
                                       "status": "name"}
        r = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "rawPath": "/admin/api/numbers",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None, "body": ""}, None)
        rows = json.loads(r["body"])
        self.assertEqual(rows[0]["number"], "+16095550123")
        self.assertEqual(rows[0]["name"], "Dr Smith")


class TabIcon(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_the_tab_has_an_icon(self):
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', self.page())

    def test_the_icon_is_valid_svg(self):
        import re, urllib.parse, xml.dom.minidom
        href = re.search(r'<link rel="icon" href="([^"]+)"', self.page()).group(1)
        xml.dom.minidom.parseString(urllib.parse.unquote(href.split(",", 1)[1]))

    def test_the_installed_app_uses_the_same_mark(self):
        import re
        page_icon = re.search(r'<link rel="icon" href="([^"]+)"', self.page()).group(1)
        man = json.loads(h.lambda_handler(
            {"requestContext": {"http": {"method": "GET"}},
             "rawPath": "/manifest.webmanifest", "headers": {}, "body": ""},
            None)["body"])
        self.assertEqual(man["icons"][0]["src"], page_icon)


class ScheduledContactSync(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.GOOGLE_CLIENT_ID, h.GOOGLE_CLIENT_SECRET = "cid", "csec"
        self.db["STATE#gtoken"] = {"pk": "STATE#gtoken", "refresh_token": "RT"}
        import sys, types
        fake = types.ModuleType("gcontacts")
        fake.access_token = lambda *a: "AT"
        fake.connections = lambda token, page_limit=20: [("+12155551234", "Mom")]
        fake.other_contacts = lambda token, page_limit=20: []
        sys.modules["gcontacts"] = fake
        self.real_poll = h.poll_gmail
        h.poll_gmail = lambda: {"polled": 0}

    def tearDown(self):
        import sys
        sys.modules.pop("gcontacts", None)
        h.poll_gmail = self.real_poll
        h.GOOGLE_CLIENT_ID = h.GOOGLE_CLIENT_SECRET = ""

    def ask_later(self):
        r = h.lambda_handler({"requestContext": {"http": {"method": "POST"}},
                              "rawPath": "/admin/api/contacts-sync",
                              "headers": {"x-admin-token": "secret"},
                              "queryStringParameters": None,
                              "body": json.dumps({"later": True})}, None)
        return json.loads(r["body"])

    def test_asking_later_schedules_rather_than_syncing_now(self):
        out = self.ask_later()
        self.assertEqual(out["due_in_secs"], 300)
        self.assertIn("STATE#gsyncdue", self.db)
        self.assertNotIn("NUM#+12155551234", self.db)     # nothing synced yet

    def test_the_tick_does_nothing_before_it_is_due(self):
        self.ask_later()
        h.lambda_handler({"poll": True}, None)
        self.assertNotIn("NUM#+12155551234", self.db)
        self.assertIn("STATE#gsyncdue", self.db)

    def test_the_tick_syncs_once_it_is_due_then_forgets(self):
        self.ask_later()
        self.db["STATE#gsyncdue"]["due"] = int(h.time.time()) - 1
        out = h.lambda_handler({"poll": True}, None)
        self.assertEqual(self.db["NUM#+12155551234"]["name"], "Mom")
        self.assertIn("contacts", out)
        self.assertNotIn("STATE#gsyncdue", self.db)

    def test_a_failing_sync_does_not_retry_forever(self):
        import sys
        sys.modules["gcontacts"].access_token = lambda *a: (_ for _ in ()).throw(
            RuntimeError("boom"))
        self.ask_later()
        self.db["STATE#gsyncdue"]["due"] = int(h.time.time()) - 1
        h.lambda_handler({"poll": True}, None)
        self.assertNotIn("STATE#gsyncdue", self.db)

    def test_a_plain_tick_with_nothing_scheduled_is_unchanged(self):
        out = h.lambda_handler({"poll": True}, None)
        self.assertEqual(out, {"polled": 0})


class EmojiPad(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_both_reply_boxes_have_an_emoji_button(self):
        page = self.page()
        self.assertIn("emojiPad('reply'", page)
        self.assertIn("emojiPad('r-${esc(g.key)}'", page)

    def test_the_pad_inserts_at_the_cursor_not_the_end(self):
        fn = self.page().split("function insertEmoji")[1].split("async function")[0]
        self.assertIn("box.selectionStart", fn)
        self.assertIn("box.value.slice(0, a) + ch + box.value.slice(b)", fn)

    def test_the_set_is_curated_and_covers_the_obvious_ones(self):
        page = self.page()
        for e in ["👍", "❤️", "📞", "💊", "✅", "🎉"]:
            self.assertIn(e, page, e)


class Email(Base):
    def setUp(self):
        super().setUp()
        h.ADMIN_TOKEN = "secret"
        h.GMAIL_USER, h.GMAIL_APP_PASSWORD = "me@gmail.com", "pw"
        h.WORK_USER, h.WORK_PASSWORD = "me@anchorfhc.com", "pw2"
        self.fetched, self.sent = {}, []
        import sys, types
        fake = types.ModuleType("mailbox")

        def fetch(user, password, since=0, want_body=True, limit=60, lookback_days=30):
            self.fetched[user] = want_body
            return list(self.mail.get(user, [])), 10
        fake.fetch = fetch
        fake.send = lambda u, p, to, subj, text, irt="": self.sent.append((u, to, subj, text))
        sys.modules["mailbox"] = fake
        self.mail = {
            "me@gmail.com": [{"uid": 1, "from_addr": "mary@x.com", "from_name": "Mary",
                              "subject": "lunch?", "ts": "2026-11-20T10:00:00-05:00",
                              "body": "are you free", "message_id": "<a>", "bulk": False}],
            "me@anchorfhc.com": [{"uid": 2, "from_addr": "pt@x.com", "from_name": "Pt",
                                  "subject": "my results", "ts": "2026-11-20T11:00:00-05:00",
                                  "body": "", "message_id": "<b>", "bulk": False},
                                 {"uid": 3, "from_addr": "news@x.com", "from_name": "News",
                                  "subject": "deals", "ts": "2026-11-20T09:00:00-05:00",
                                  "body": "", "message_id": "<c>", "bulk": True}],
        }

    def tearDown(self):
        import sys
        sys.modules.pop("mailbox", None)
        h.GMAIL_USER = h.GMAIL_APP_PASSWORD = h.WORK_USER = h.WORK_PASSWORD = ""

    def test_personal_is_fetched_with_bodies_and_practice_without(self):
        h.poll_mail()
        self.assertEqual(self.fetched["me@gmail.com"], True)
        self.assertEqual(self.fetched["me@anchorfhc.com"], False)

    def test_practice_mail_is_stored_without_a_body(self):
        h.poll_mail()
        work = self.db["EMAIL#practice#2"]
        self.assertEqual(work["subject"], "my results")
        self.assertEqual(work["body"], "")
        self.assertTrue(work["headers_only"])

    def test_personal_mail_keeps_its_body(self):
        h.poll_mail()
        self.assertEqual(self.db["EMAIL#personal#1"]["body"], "are you free")
        self.assertFalse(self.db["EMAIL#personal#1"]["headers_only"])

    def test_google_voice_notifications_do_not_appear_as_email(self):
        self.mail["me@gmail.com"].append(
            {"uid": 4, "from_addr": "reply+abc@txt.voice.google.com",
             "from_name": "Mary", "subject": "New text message",
             "ts": "2026-11-20T12:00:00-05:00", "body": "hi", "message_id": "<d>",
             "bulk": False})
        h.poll_mail()
        self.assertNotIn("EMAIL#personal#4", self.db)
        self.assertIn("EMAIL#personal#1", self.db)   # ordinary mail still lands

    def test_bulk_mail_is_dropped(self):
        h.poll_mail()
        self.assertNotIn("EMAIL#practice#3", self.db)

    def test_replying_from_the_practice_account_is_refused(self):
        ok, note = h.send_mail("practice", "pt@x.com", "Re", "hello")
        self.assertFalse(ok)
        self.assertIn("read-only", note)
        self.assertEqual(self.sent, [])

    def test_replying_from_personal_sends_and_is_recorded(self):
        ok, note = h.send_mail("personal", "mary@x.com", "Re: lunch?", "yes")
        self.assertTrue(ok, note)
        self.assertEqual(self.sent[0][:3], ("me@gmail.com", "mary@x.com", "Re: lunch?"))
        out = [i for i in self.db.values() if i.get("dir") == "out"][0]
        self.assertEqual(out["to_addr"], "mary@x.com")

    def test_a_broken_mailbox_does_not_stop_the_other(self):
        import sys
        real = sys.modules["mailbox"].fetch

        def half(user, password, since=0, want_body=True, **kw):
            if user == "me@anchorfhc.com":
                raise OSError("login failed")
            return real(user, password, since, want_body, **kw)
        sys.modules["mailbox"].fetch = half
        out = h.poll_mail()
        self.assertIn("login failed", out["accounts"]["practice"]["error"])
        self.assertEqual(out["accounts"]["personal"]["kept"], 1)


class _Resp(io.BytesIO):
    """Just enough of an HTTP response for fetch_media."""

    def __init__(self, data, ctype):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}


class Attachments(Base):
    def setUp(self):
        super().setUp()
        self.objects, self.posts = {}, []
        h.put_media = lambda data, key, ctype: self.objects.__setitem__(key, (data, ctype))
        h.media_url = lambda key, expires=3600: f"https://signed.example/{key}"
        self.real = h.urllib.request.urlopen
        self.body = b"\xff\xd8JPEGBYTES"

        def fake(req, timeout=0):
            url = req if isinstance(req, str) else req.full_url
            if "api.telnyx.com" in url:
                self.posts.append(json.loads(req.data))
                return _Resp(b"{}", "application/json")
            return _Resp(self.body, "image/jpeg")

        h.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(h.urllib.request, "urlopen", self.real))

    def msg(self):
        return [i for i in self.db.values() if i.get("gsi1pk") == "MSGS"][0]

    def mms(self, media):
        h.handle_webhook({"data": {"event_type": "message.received", "id": "e1",
                                   "payload": {"id": "m1", "text": "look",
                                               "from": {"phone_number": "+12155551234"},
                                               "to": [{"phone_number": "+16095550001"}],
                                               "media": media}}})

    def test_inbound_mms_is_copied_into_our_own_bucket(self):
        self.mms([{"url": "https://media.telnyx.com/x", "content_type": "image/jpeg"}])
        m = self.msg()
        self.assertEqual(len(m["files"]), 1)
        key = m["files"][0]["key"]
        self.assertTrue(key.startswith("media/"), key)
        self.assertTrue(key.endswith(".jpg"), key)
        self.assertEqual(self.objects[key], (self.body, "image/jpeg"))
        # Telnyx media URLs expire, so nothing may depend on them later.
        self.assertNotIn("media.telnyx.com", json.dumps(m))

    def test_an_attachment_that_will_not_save_does_not_lose_the_text(self):
        def boom(req, timeout=0):
            raise OSError("gone")
        h.urllib.request.urlopen = boom
        self.mms([{"url": "https://media.telnyx.com/x", "content_type": "image/jpeg"}])
        m = self.msg()
        self.assertEqual(m["text"], "look")
        self.assertEqual(m.get("files", []), [])
        self.assertEqual(m["media"], 1)        # the panel shows one is missing

    def test_oversize_attachment_is_refused(self):
        self.body = b"x" * (h.MEDIA_MAX + 1)
        self.mms([{"url": "https://media.telnyx.com/big", "content_type": "image/jpeg"}])
        self.assertEqual(self.msg().get("files", []), [])
        self.assertEqual(self.objects, {})

    def test_unknown_content_type_still_gets_a_key(self):
        self.mms([{"url": "https://media.telnyx.com/x", "content_type": "application/zip"}])
        self.assertTrue(self.msg()["files"][0]["key"].endswith(".bin"))

    def test_gateway_attachment_urls_are_stored(self):
        h.on_gateway_post({"id": "g1", "payload": {
            "phoneNumber": "+12155551234", "message": "pic",
            "attachments": [{"url": "https://gw.local/1", "contentType": "image/png",
                             "name": "shot.png"}]}})
        f = self.msg()["files"][0]
        self.assertEqual(f["name"], "shot.png")
        self.assertIn(f["key"], self.objects)

    def test_google_voice_image_attachment_is_parsed_and_stored(self):
        import email.message
        import types
        note = email.message.EmailMessage()
        note["From"] = "Mary (SMS) <+12155551234.ab@txt.voice.google.com>"
        note["Reply-To"] = "abc@txt.voice.google.com"
        note["Subject"] = "New text message from +1 215 555 1234"
        note["Message-ID"] = "<x@mail>"
        note.set_content("look at this")
        note.add_attachment(b"PNGBYTES", maintype="image", subtype="png",
                            filename="pic.png")
        import gvmail
        parsed = gvmail.parse(note)
        self.assertEqual(len(parsed["files"]), 1)
        self.assertEqual(parsed["text"], "look at this")

        h.GMAIL_USER, h.GMAIL_APP_PASSWORD = "me@gmail.com", "pw"
        fake = types.ModuleType("gvmail")
        parsed["uid"] = 7
        fake.fetch = lambda u, p, since=0: ([parsed], 7)
        sys.modules["gvmail"] = fake
        try:
            h.poll_gmail()
        finally:
            sys.modules["gvmail"] = gvmail
        m = self.msg()
        self.assertEqual(self.objects[m["files"][0]["key"]], (b"PNGBYTES", "image/png"))

    def test_picture_goes_out_as_mms_on_the_telnyx_line(self):
        h.API_KEY = "KEY"
        h.store_message("in-1", "+12155551234", "+16095550001", "Cell", "hi",
                        src="telnyx")
        ok, note = h.send_text("+12155551234", "here you go", files=[
            {"data": b"PNGBYTES", "content_type": "image/png", "name": "pic.png"}])
        self.assertTrue(ok, note)
        body = self.posts[0]
        self.assertEqual((body["from"], body["to"]), ("+16095550001", "+12155551234"))
        self.assertEqual(body["text"], "here you go")
        self.assertEqual(len(body["media_urls"]), 1)
        self.assertTrue(body["media_urls"][0].startswith("https://signed.example/media/"))
        out = [i for i in self.db.values() if i.get("dir") == "out"][0]
        self.assertEqual(out["files"][0]["type"], "image/png")

    def test_picture_on_a_google_voice_thread_leaves_on_the_gv_facing_did(self):
        h.API_KEY = "KEY"
        h.store_message("gv-1", "+12155551234", "", "GV", "hi", src="gv",
                        relay="abc@txt.voice.google.com")
        ok, note = h.send_text("+12155551234", "see this",
                               files=[{"data": b"x", "content_type": "image/png"}])
        self.assertTrue(ok, note)
        self.assertEqual(self.posts[0]["from"], "+16095550002")   # the GV line
        self.assertEqual(len(self.posts[0]["media_urls"]), 1)
        # The recipient sees a different number, so the panel has to say so.
        self.assertIn("Google Voice can't send pictures", note)

    def test_picture_on_a_cell_thread_leaves_on_the_cell_facing_did(self):
        h.API_KEY = "KEY"
        h.GATEWAY_USER = h.GATEWAY_PASS = "x"
        h.store_message("c-1", "+12155551234", "", "Cell", "hi", src="cell")
        ok, note = h.send_text("+12155551234", "see this",
                               files=[{"data": b"x", "content_type": "image/png"}])
        self.assertTrue(ok, note)
        self.assertEqual(self.posts[0]["from"], "+16095550001")   # the cell line
        self.assertIn("your cell can't send pictures", note)

    def test_a_plain_text_reply_still_goes_out_its_own_line(self):
        h.API_KEY = "KEY"
        h.store_message("gv-1", "+12155551234", "", "GV", "hi", src="gv",
                        relay="abc@txt.voice.google.com")
        h.send_mail = lambda *a, **k: (_ for _ in ()).throw(AssertionError("via gv"))
        ok, note = h.send_text("+12155551234", "words only")
        # No MMS call: text on a GV thread must not be rerouted to Telnyx.
        self.assertEqual(self.posts, [])

    def test_text_only_send_still_works_untouched(self):
        h.GATEWAY_USER = h.GATEWAY_PASS = "x"
        h.store_message("c-1", "+12155551234", "", "Cell", "hi", src="cell")
        ok, note = h.send_text("+12155551234", "plain")
        self.assertTrue(ok, note)


class AttachmentApi(Base):
    def call(self, method, path, body=None, query=None):
        return h.lambda_handler({
            "requestContext": {"http": {"method": method}}, "rawPath": path,
            "headers": {"x-admin-token": "secret"},
            "queryStringParameters": query,
            "body": json.dumps(body) if body else "",
        }, None)

    def test_media_endpoint_signs_a_known_key(self):
        h.media_url = lambda key, expires=3600: f"https://signed.example/{key}"
        r = self.call("GET", "/admin/api/media", query={"key": "media/m1-0.jpg"})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["url"],
                         "https://signed.example/media/m1-0.jpg")

    def test_media_endpoint_refuses_a_path_outside_media(self):
        for key in ("../template.yaml", "voicemail/x.mp3", "media/../secrets"):
            r = self.call("GET", "/admin/api/media", query={"key": key})
            self.assertEqual(r["statusCode"], 400, key)

    def test_send_rejects_an_attachment_that_is_too_big(self):
        import base64
        payload = base64.b64encode(b"x" * (h.MEDIA_MAX + 1)).decode()
        r = self.call("POST", "/admin/api/send", {
            "to": "+12155551234", "text": "hi",
            "files": [{"name": "big.jpg", "type": "image/jpeg", "data": payload}]})
        self.assertEqual(r["statusCode"], 400)
        self.assertIn("too big", json.loads(r["body"])["error"])

    def test_send_accepts_an_attachment_with_no_text(self):
        seen = {}
        real = h.send_text
        self.addCleanup(lambda: setattr(h, "send_text", real))
        h.send_text = lambda to, text, via="", files=None: (
            seen.update(to=to, text=text, files=files), (True, "sent"))[1]
        r = self.call("POST", "/admin/api/send", {
            "to": "+12155551234", "files": [
                {"name": "p.png", "type": "image/png", "data": "UE5HQllURVM="}]})
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(seen["files"][0]["data"], b"PNGBYTES")
        self.assertEqual(seen["text"], "")


class AttachmentPanel(Base):
    def page(self):
        r = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "rawPath": "/admin", "headers": {}, "body": ""}, None)
        return r["body"]

    def test_the_attach_control_is_a_native_label_not_a_scripted_click(self):
        page = self.page()
        # A scripted click on a hidden input is unreliable on Android.
        self.assertNotIn("pickAttach", page)
        self.assertIn('for="filePick"', page)
        self.assertIn('id="filePick"', page)
        self.assertNotIn('type="file" id="filePick" hidden', page)

    def test_a_failed_read_and_a_thrown_error_both_reach_the_screen(self):
        page = self.page()
        self.assertIn("Could not read that file", page)
        self.assertIn("addEventListener('error'", page)
        self.assertIn("unhandledrejection", page)
        self.assertIn("Nothing to send yet", page)

    def test_composer_offers_an_attach_control_and_renders_files(self):
        page = self.page()
        self.assertIn("filesHtml(m)", page)
        self.assertIn("api/media", page.replace("'media?key='", "api/media"))

    def test_images_are_resolved_through_the_signed_url_endpoint(self):
        page = self.page()
        self.assertIn("loadShots", page)
        self.assertIn("mediaUrls[key]", page)

    def test_attachments_are_not_pinned_to_pixels_for_width(self):
        # A sizeable font must not be squeezed by a fixed-width picture column.
        self.assertIn("max-width: min(260px, 70vw)", self.page())


class NavBar(Base):
    def page(self):
        r = h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                              "rawPath": "/admin", "headers": {}, "body": ""}, None)
        return r["body"]

    def test_the_ellipsis_menu_is_gone(self):
        page = self.page()
        self.assertNotIn("&#8942;", page)      # the vertical ellipsis
        self.assertNotIn("toggleMenu", page)

    def test_every_destination_has_its_own_colour_tile(self):
        page = self.page()
        for tile in ("tabInbox", "tabMail", "tabNew", "tabNums", "tabArch",
                     "tabStatus"):
            self.assertIn(f'id="{tile}"', page, tile)
        # One distinct colour each, or the tiles stop being a cue.
        inks = set(re.findall(r"--ink:(#[0-9a-f]{6})", page))
        self.assertEqual(len(inks), 6, inks)

    def test_the_nav_is_shown_rather_than_toggled(self):
        page = self.page()
        self.assertIn("$('menu').hidden = false", page)
        self.assertNotIn("$('menu').hidden = true", page.replace(
            "for (const id of ['hdr', 'menu'", "for (const id of ['hdr', 'x'"))

    def test_the_tile_for_the_open_tab_is_marked(self):
        self.assertIn("function navActive()", self.page())


class InstallableApp(Base):
    def get(self, path):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": path, "headers": {}, "body": ""}, None)

    def test_manifest_has_what_android_needs_to_install(self):
        r = self.get("/manifest.webmanifest")
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(r["headers"]["content-type"], "application/manifest+json")
        m = json.loads(r["body"])
        self.assertEqual(m["display"], "standalone")
        self.assertEqual(m["start_url"], "/admin")
        sizes = {i["sizes"] for i in m["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)       # splash screen
        self.assertIn("maskable", {i.get("purpose") for i in m["icons"]})

    def test_service_worker_is_served_as_javascript(self):
        r = self.get("/sw.js")
        self.assertEqual(r["statusCode"], 200)
        self.assertIn("javascript", r["headers"]["content-type"])
        self.assertIn("addEventListener('fetch'", r["body"])

    def test_the_worker_never_caches_live_data(self):
        body = self.get("/sw.js")["body"]
        self.assertIn("/admin/api/", body)
        self.assertIn("never cache live data", body)
        # Fresh first: the panel is redeployed too often to serve a stale shell.
        self.assertLess(body.index("await fetch(event.request)"),
                        body.index("caches.match"))

    def test_the_panel_registers_the_worker(self):
        self.assertIn("serviceWorker.register('/sw.js')", self.get("/admin")["body"])

    def test_launcher_shortcuts_open_the_right_tab(self):
        m = json.loads(self.get("/manifest.webmanifest")["body"])
        self.assertEqual({s["url"] for s in m["shortcuts"]},
                         {"/admin?go=new", "/admin?go=mail"})
        self.assertIn("get('go')", self.get("/admin")["body"])


class ContactLookup(Base):
    def page(self):
        return h.lambda_handler({"requestContext": {"http": {"method": "GET"}},
                                 "rawPath": "/admin", "headers": {}, "body": ""},
                                None)["body"]

    def test_the_new_message_field_takes_a_name(self):
        page = self.page()
        self.assertIn('placeholder="Name or phone number"', page)
        self.assertIn('list="book"', page)
        self.assertIn('<datalist id="book">', page)
        self.assertIn("function fillBook()", page)

    def test_both_actions_go_through_the_resolver(self):
        page = self.page()
        self.assertIn("const to = resolveTo(), text", page)      # send
        self.assertIn("async function callNew() {\n  const to = resolveTo();", page)
        self.assertNotIn("const to = $('newTo').value.trim();", page)

    def test_an_ambiguous_or_unknown_name_is_reported_not_guessed(self):
        page = self.page()
        self.assertIn("keep typing or pick one", page)
        self.assertIn("No contact matches", page)

    def test_the_book_loads_even_from_a_launcher_shortcut(self):
        self.assertIn("if (!Object.keys(names).length)", self.page())

    def test_numbers_the_resolver_hands_back_are_understood(self):
        # Whatever shape the field or the picker produces must normalize.
        for typed in ("6095550123", "(609) 555-0123", "+16095550123"):
            self.assertEqual(h.normalize(typed), "+16095550123", typed)


class MailboxParsing(Base):
    def msg(self, raw):
        import email as e
        import sys
        sys.path.insert(0, "src")
        import mailbox
        return mailbox, e.message_from_string(raw)

    def test_newsletters_are_recognised(self):
        mb, m = self.msg("From: News <news@x.com>\nList-Unsubscribe: <http://x>\n\nhi")
        self.assertTrue(mb.is_bulk(m))

    def test_a_person_is_not(self):
        mb, m = self.msg("From: Mary <mary@x.com>\nSubject: lunch\n\nhi")
        self.assertFalse(mb.is_bulk(m))

    def test_no_reply_senders_are_bulk(self):
        mb, m = self.msg("From: no-reply@bank.com\n\nhi")
        self.assertTrue(mb.is_bulk(m))

    def test_headers_only_never_carries_the_body(self):
        mb, m = self.msg("From: Pt <pt@x.com>\nSubject: results\n\nmy blood pressure is")
        self.assertEqual(mb.parse(m, want_body=False)["body"], "")
        self.assertIn("blood pressure", mb.parse(m, want_body=True)["body"])
