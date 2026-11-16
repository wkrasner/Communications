"""Simulates Telnyx webhook sequences against the handler with fake storage and a fake Telnyx."""
import json
import os
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
