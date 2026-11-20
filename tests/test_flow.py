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
        for glyph, word in [("&#9742;", "Call"), ("&#10003;", "Allow"),
                            ("&#8856;", "Block"), ("&#128465;", "Delete"),
                            ("&#9654;", "Voicemail"), ("&#10148;", "Send"),
                            ("&#9993;", "Inbox"), ("&#9881;", "Status")]:
            self.assertIn(f"{glyph} {word}", page, word)


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
