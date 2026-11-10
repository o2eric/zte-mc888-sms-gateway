"""Testy bramki powiadomień (sms_gateway.py), etap 1: klienci, ponowienia, stany wysyłki.

Modem nie jest dotykany: send_sms_zte jest podmieniany. Stan i konfiguracja idą do katalogu tymczasowego.
Uruchomienie:  python -m pytest -q test_sms_gateway.py
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import sms_gateway as gw

LEGACY_TOKEN = "legacy-token-for-tests"
NEW_TOKEN = "new-client-token-for-tests"


def base_config(**over):
    cfg = {
        "live": True, "daily_limit": 8, "quiet_hours": ["00:00", "00:00"], "max_chars": 200,
        "allow_clients": ["127.0.0.1"], "channels": {"sms": True, "whatsapp": False},
        "recipients": {"contact1": {"sms": "+48600000001"}, "contact2": {"sms": "+48600000002"}},
        "clients": {
            "alarm": {"token_sha256": gw.token_hash(NEW_TOKEN), "allow_ips": ["127.0.0.1"],
                      "recipients": ["contact1"], "retry": True, "allow_high": False, "daily_limit": 2},
        },
        "retry": {"attempts": 3, "backoff_s": [0, 0], "max_age_s": 3600},
    }
    cfg.update(over)
    return cfg


class GatewayCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = os.path.join(self.tmp.name, "cfg.json")
        self.token_path = os.path.join(self.tmp.name, "token")
        with open(self.token_path, "w") as fh:
            fh.write(LEGACY_TOKEN)
        self.write_config(base_config())
        for name, val in (("CONFIG", self.cfg_path), ("TOKEN_FILE", self.token_path), ("STATE_DIR", self.tmp.name)):
            p = mock.patch.object(gw, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.sms_calls = []
        self.sms_result = ["sent"]  # kolejne wyniki dla send_sms_zte

        def fake_sms(number, text):
            self.sms_calls.append((number, text))
            return self.sms_result.pop(0) if len(self.sms_result) > 1 else self.sms_result[0]

        p = mock.patch.object(gw, "send_sms_zte", fake_sms)
        p.start()
        self.addCleanup(p.stop)

    def write_config(self, cfg):
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def ledger_results(self):
        path = os.path.join(self.tmp.name, "communication.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(line)["result"] for line in open(path, encoding="utf-8")]

    def alarm(self):
        return gw.client_profile("alarm", base_config()["clients"]["alarm"])


class LegacyClient(GatewayCase):
    """LegacyClient: zachowanie musi zostać bez zmian."""

    def test_sent_and_duplicate(self):
        out = gw.notify({"key": "k1", "text": "treść"})
        self.assertEqual(out["results"]["contact1"]["sms"], "sent")
        self.assertEqual(out["results"]["contact2"]["sms"], "sent")
        again = gw.notify({"key": "k1", "text": "treść"})
        self.assertEqual(again["results"]["contact1"]["sms"], "duplicate")
        self.assertEqual(len(self.sms_calls), 2)

    def test_unconfirmed_is_reported_as_sent_but_not_retried(self):
        self.sms_result = ["sent_unconfirmed"]
        out = gw.notify({"key": "k2", "text": "x"}, gw.LEGACY_PROFILE)
        self.assertEqual(out["results"]["contact1"]["sms"], "sent")
        self.assertIn("sent_unconfirmed", self.ledger_results())
        self.assertEqual(gw.load_state()["queue"], [])

    def test_failed_stays_failed_without_gateway_retry(self):
        self.sms_result = ["failed"]
        out = gw.notify({"key": "k3", "text": "x"})
        self.assertEqual(out["results"]["contact1"]["sms"], "failed")
        self.assertEqual(gw.load_state()["queue"], [])
        gw.process_retries()
        self.assertEqual(len(self.sms_calls), 2)  # po jednej próbie na odbiorcę, bez ponowień bramki
        # LegacyClient ponawia sam tym samym kluczem i nie dostaje "duplicate"
        self.sms_result = ["sent"]
        again = gw.notify({"key": "k3", "text": "x"})
        self.assertEqual(again["results"]["contact1"]["sms"], "sent")

    def test_legacy_dedupe_key_has_no_client_prefix(self):
        gw.notify({"key": "k4", "text": "x"})
        self.assertIn("k4|contact1|sms", gw.load_state()["sent"])

    def test_dry_run_when_not_live(self):
        self.write_config(base_config(live=False))
        out = gw.notify({"key": "k5", "text": "x"})
        self.assertEqual(out["results"]["contact1"]["sms"], "dry_run")
        self.assertEqual(self.sms_calls, [])

    def test_quiet_hours_defer_normal_but_not_high(self):
        self.write_config(base_config(quiet_hours=["00:00", "23:59"]))
        self.assertEqual(gw.notify({"key": "k6", "text": "x"})["results"]["contact1"]["sms"], "deferred")
        self.assertEqual(gw.notify({"key": "k7", "text": "x", "priority": "high"})["results"]["contact1"]["sms"], "sent")

    def test_unknown_recipient(self):
        out = gw.notify({"key": "k8", "text": "x", "to": ["ktos"]})
        self.assertEqual(out["results"]["ktos"]["status"], "unknown_recipient")


class NewClient(GatewayCase):
    def test_default_recipients_limited_to_allowed(self):
        out = gw.notify({"key": "n1", "text": "x"}, self.alarm())
        self.assertEqual(list(out["results"]), ["contact1"])
        self.assertEqual(out["client"], "alarm")

    def test_forbidden_recipient(self):
        out = gw.notify({"key": "n2", "text": "x", "to": ["contact2"]}, self.alarm())
        self.assertEqual(out["results"]["contact2"]["status"], "forbidden_recipient")
        self.assertEqual(self.sms_calls, [])

    def test_high_priority_is_downgraded_without_permission(self):
        self.write_config(base_config(quiet_hours=["00:00", "23:59"]))
        out = gw.notify({"key": "n3", "text": "x", "priority": "high"}, self.alarm())
        self.assertEqual(out["results"]["contact1"]["sms"], "deferred")

    def test_client_daily_limit(self):
        for i in range(2):
            r = gw.notify({"key": f"n4-{i}", "text": "x"}, self.alarm())
            self.assertEqual(r["results"]["contact1"]["sms"], "sent")
        r = gw.notify({"key": "n4-2", "text": "x"}, self.alarm())
        self.assertEqual(r["results"]["contact1"]["sms"], "limit")

    def test_dedupe_is_per_client(self):
        gw.notify({"key": "same", "text": "x"})  # legacy
        r = gw.notify({"key": "same", "text": "x"}, self.alarm())
        self.assertEqual(r["results"]["contact1"]["sms"], "sent")

    def test_unconfirmed_is_visible_to_new_client(self):
        self.sms_result = ["sent_unconfirmed"]
        r = gw.notify({"key": "n5", "text": "x"}, self.alarm())
        self.assertEqual(r["results"]["contact1"]["sms"], "sent_unconfirmed")

    def test_failed_is_queued_then_retried(self):
        self.sms_result = ["failed", "sent"]
        r = gw.notify({"key": "n6", "text": "x"}, self.alarm())
        self.assertEqual(r["results"]["contact1"]["sms"], "queued")
        self.assertEqual(len(gw.load_state()["queue"]), 1)
        # powtórzenie tym samym kluczem nie wysyła drugi raz
        r2 = gw.notify({"key": "n6", "text": "x"}, self.alarm())
        self.assertEqual(r2["results"]["contact1"]["sms"], "queued")
        self.assertEqual(len(self.sms_calls), 1)
        gw.process_retries()
        state = gw.load_state()
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["results"]["alarm|n6"]["to"]["contact1"]["sms"], "sent")
        self.assertIn("sent_retry", self.ledger_results())

    def test_retry_gives_up_after_attempts(self):
        self.sms_result = ["failed"]
        gw.notify({"key": "n7", "text": "x"}, self.alarm())
        for _ in range(5):
            gw.process_retries()
        state = gw.load_state()
        self.assertEqual(state["queue"], [])
        self.assertEqual(state["results"]["alarm|n7"]["to"]["contact1"]["sms"], "failed_final")
        self.assertEqual(len(self.sms_calls), 3)  # attempts = 3

    def test_retry_does_nothing_in_dry_run(self):
        self.sms_result = ["failed"]
        gw.notify({"key": "n8", "text": "x"}, self.alarm())
        self.write_config(base_config(live=False))
        gw.process_retries()
        self.assertEqual(len(gw.load_state()["queue"]), 1)

    def test_sent_today_ignores_client_counters(self):
        gw.notify({"key": "n9", "text": "x"}, self.alarm())
        today = gw.datetime.now(gw.TZ).strftime("%Y-%m-%d")
        self.assertEqual(gw.sent_today(gw.load_state(), today), {"sms": 1})


class Contract(GatewayCase):
    """Kontrakt API opisany w dokumentacji (docs/reference/sms-gateway/api)."""

    def test_result_keeps_original_state_after_duplicate(self):
        gw.notify({"key": "c1", "text": "x"}, self.alarm())
        again = gw.notify({"key": "c1", "text": "x"}, self.alarm())
        self.assertEqual(again["results"]["contact1"]["sms"], "duplicate")
        self.assertEqual(gw.load_state()["results"]["alarm|c1"]["to"]["contact1"]["sms"], "sent")

    def test_to_as_string_is_one_recipient(self):
        out = gw.notify({"key": "c2", "text": "x", "to": "contact1"}, self.alarm())
        self.assertEqual(out["results"], {"contact1": {"sms": "sent"}})

    def test_to_of_wrong_type_is_bad_request(self):
        self.assertEqual(gw.notify({"key": "c3", "text": "x", "to": 5}, self.alarm()), {"error": "bad request"})
        self.assertEqual(gw.notify({"key": "c4", "text": "x", "to": [1]}, self.alarm()), {"error": "bad request"})

    def test_text_whitespace_collapsed_and_truncated(self):
        gw.notify({"key": "c5", "text": "A   B\n\nC " + "x" * 400}, self.alarm())
        self.assertEqual(self.sms_calls[0][1][:5], "A B C")
        self.assertEqual(len(self.sms_calls[0][1]), 200)

    def test_kind_is_sanitized_in_ledger(self):
        gw.notify({"key": "c6", "text": "x", "kind": "Test-Kind 1!"}, self.alarm())
        path = os.path.join(self.tmp.name, "communication.jsonl")
        self.assertEqual(json.loads(open(path, encoding="utf-8").readline())["kind"], "testkind")


class Identify(GatewayCase):
    def cfg(self):
        return gw.load_config()

    def test_new_client_ok(self):
        profile, code = gw.identify("127.0.0.1", "Bearer " + NEW_TOKEN, self.cfg())
        self.assertEqual((profile["name"], code), ("alarm", 0))

    def test_new_client_wrong_ip_is_403(self):
        self.assertEqual(gw.identify("10.0.0.5", "Bearer " + NEW_TOKEN, self.cfg()), (None, 403))

    def test_legacy_ok_and_wrong_token_and_wrong_ip(self):
        self.assertEqual(gw.identify("127.0.0.1", "Bearer " + LEGACY_TOKEN, self.cfg())[0]["name"], "legacy")
        self.assertEqual(gw.identify("127.0.0.1", "Bearer zly", self.cfg()), (None, 401))
        self.assertEqual(gw.identify("10.0.0.5", "Bearer " + LEGACY_TOKEN, self.cfg()), (None, 403))
        self.assertEqual(gw.identify("127.0.0.1", "", self.cfg()), (None, 401))

    def test_legacy_token_does_not_unlock_new_client_scope(self):
        # token klienta "alarm" nie zadziała jako token legacy i odwrotnie
        profile, _ = gw.identify("127.0.0.1", "Bearer " + LEGACY_TOKEN, self.cfg())
        self.assertTrue(profile["legacy"])
        profile, _ = gw.identify("127.0.0.1", "Bearer " + NEW_TOKEN, self.cfg())
        self.assertFalse(profile["legacy"])


class Http(GatewayCase):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self, method, path, token=None, body=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_status_per_client(self):
        code, out = self.call("GET", "/v1/status", NEW_TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(out["client"], "alarm")
        self.assertEqual(list(out["recipients"]), ["contact1"])
        code, out = self.call("GET", "/v1/status", LEGACY_TOKEN)
        self.assertEqual(sorted(out["recipients"]), ["contact1", "contact2"])

    def test_unauthorized(self):
        self.assertEqual(self.call("GET", "/v1/status")[0], 401)
        self.assertEqual(self.call("GET", "/v1/status", "zly")[0], 401)

    def test_post_and_result_lookup(self):
        code, out = self.call("POST", "/v1/notify", NEW_TOKEN, {"key": "h1", "text": "cześć"})
        self.assertEqual(code, 200)
        self.assertEqual(out["results"]["contact1"]["sms"], "sent")
        code, res = self.call("GET", "/v1/result/h1", NEW_TOKEN)
        self.assertEqual((code, res["results"]["contact1"]["sms"]), (200, "sent"))
        # inny klient nie widzi cudzego wyniku
        self.assertEqual(self.call("GET", "/v1/result/h1", LEGACY_TOKEN)[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/v1/notify", NEW_TOKEN, {"key": "bad key!", "text": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/notify", NEW_TOKEN, {"key": "ok", "text": ""})[0], 400)
        self.assertEqual(self.call("GET", "/v1/nope", NEW_TOKEN)[0], 404)


class NewToken(unittest.TestCase):
    def test_hash_is_sha256_hex(self):
        self.assertEqual(len(gw.token_hash("abc")), 64)
        self.assertEqual(gw.token_hash("abc"), gw.token_hash("abc"))
        self.assertNotEqual(gw.token_hash("abc"), gw.token_hash("abd"))


if __name__ == "__main__":
    unittest.main()
