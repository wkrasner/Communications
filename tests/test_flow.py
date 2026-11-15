"""Simulates Telnyx webhook sequences against the handler with fake storage and a fake Telnyx."""
import os
import sys
import unittest

os.environ.update(CELL_DID="+16095550001", GV_DID="+16095550002",
                  RING_TARGET="sip:me@sip.telnyx.com", ADMIN_TOKEN="secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import handler as h  # noqa: E402


class Flow(unittest.TestCase):
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
        self.assertEqual(body["from"], "+12155551234")
        self.assertEqual(body["from_display_name"], "GV: Mom")
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
        self.assertEqual(body["from_display_name"], "Cell: screened")
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
