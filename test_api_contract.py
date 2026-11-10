"""Test kontraktowy API SMS: odpowiedzi prawdziwego kodu bramki muszą spełniać schematy z openapi.yaml.

Specyfikacja: docs/openapi.yaml.
Test sprawdza też, że KAŻDY przykład w specyfikacji spełnia schemat swojej odpowiedzi/żądania (dokumentacja się nie rozjeżdża).
Uruchomienie:  python -m pytest -q test_api_contract.py
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

try:
    import jsonschema
    import yaml
except ImportError:  # np. host nie ma PyYAML ani jsonschema
    raise unittest.SkipTest("brak PyYAML lub jsonschema")

import sms_gateway as gw

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATHS = [os.path.join(HERE, "docs", "openapi.yaml")]
TOKEN = "contract-token"


def load_spec():
    for p in SPEC_PATHS:
        if os.path.exists(p):
            return yaml.safe_load(open(p, encoding="utf-8"))
    raise unittest.SkipTest("brak openapi.yaml")


def validator(spec, schema):
    root = dict(schema)
    root["components"] = spec["components"]
    return jsonschema.Draft7Validator(root)


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


class SpecIsConsistent(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()

    def test_all_refs_resolve(self):
        refs = []

        def walk(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    refs.append(node["$ref"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(self.spec)
        self.assertGreater(len(refs), 5)
        for r in refs:
            target = self.spec
            for part in r.lstrip("#/").split("/"):
                self.assertIn(part, target, r)
                target = target[part]

    def test_every_example_matches_its_schema(self):
        checked = 0
        for path, ops in self.spec["paths"].items():
            for method, op in ops.items():
                bodies = []
                if "requestBody" in op:
                    bodies += list(op["requestBody"]["content"].values())
                for status, resp in op["responses"].items():
                    if "$ref" in resp:
                        resp = self.spec["components"]["responses"][resp["$ref"].split("/")[-1]]
                    bodies += list(resp.get("content", {}).values())
                for media in bodies:
                    values = []
                    if "example" in media:
                        values.append(media["example"])
                    values += [e["value"] for e in media.get("examples", {}).values()]
                    for value in values:
                        validator(self.spec, media["schema"]).validate(value)
                        checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_documented_states_equal_code_states(self):
        """Stany w specyfikacji muszą pokrywać stany, które kod potrafi zwrócić."""
        enum = set(self.spec["components"]["schemas"]["SendState"]["enum"])
        recipient = set(self.spec["components"]["schemas"]["RecipientError"]["enum"])
        source = open(os.path.join(HERE, "sms_gateway.py"), encoding="utf-8").read()
        for state in enum | recipient:
            self.assertIn(f'"{state}"', source, state)


class ResponsesMatchSchemas(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = {"live": True, "daily_limit": 8, "quiet_hours": ["00:00", "00:00"], "max_chars": 200,
               "allow_clients": ["127.0.0.1"], "channels": {"sms": True, "whatsapp": False},
               "recipients": {"contact1": {"sms": "+48600000001"}, "contact2": {"sms": "+48600000002"},
                              "contact3": {"sms": "+48600000003"}},
               "clients": {"demo": {"token_sha256": gw.token_hash(TOKEN), "allow_ips": ["127.0.0.1"],
                                    "recipients": ["contact1", "contact2"], "daily_limit": 20,
                                    "allow_high": True, "retry": True}},
               "retry": {"attempts": 2, "backoff_s": [0], "max_age_s": 60}}
        self.cfg_path = os.path.join(self.tmp.name, "cfg.json")
        json.dump(cfg, open(self.cfg_path, "w", encoding="utf-8"))
        token_path = os.path.join(self.tmp.name, "token")
        open(token_path, "w").write("legacy")
        self.modem = ["sent"]
        for name, val in (("CONFIG", self.cfg_path), ("TOKEN_FILE", token_path), ("STATE_DIR", self.tmp.name)):
            p = mock.patch.object(gw, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(gw, "send_sms_zte", lambda n, t: self.modem.pop(0) if len(self.modem) > 1 else self.modem[0])
        p.start()
        self.addCleanup(p.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self, method, path, token=TOKEN, body=None, raw=None):
        headers = {"Authorization": "Bearer " + token} if token else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def check(self, schema_name, payload):
        validator(self.spec, ref(schema_name)).validate(payload)

    def test_notify_variants(self):
        code, out = self.call("POST", "/v1/notify", body={"key": "a1", "text": "x"})
        self.assertEqual(code, 200)
        self.check("NotifyResponse", out)
        code, out = self.call("POST", "/v1/notify", body={"key": "a2", "text": "x", "to": ["contact3", "ktos", "contact1"]})
        self.check("NotifyResponse", out)
        self.assertEqual(out["results"]["contact3"], {"status": "forbidden_recipient"})
        self.assertEqual(out["results"]["ktos"], {"status": "unknown_recipient"})
        code, out = self.call("POST", "/v1/notify", body={"key": "a1", "text": "x"})  # dublet
        self.check("NotifyResponse", out)
        self.assertEqual(out["results"]["contact1"]["sms"], "duplicate")

    def test_queue_and_result(self):
        self.modem = ["failed", "sent"]
        code, out = self.call("POST", "/v1/notify", body={"key": "q1", "text": "x", "to": ["contact1"]})
        self.check("NotifyResponse", out)
        self.assertEqual(out["results"]["contact1"]["sms"], "queued")
        gw.process_retries()
        code, res = self.call("GET", "/v1/result/q1")
        self.assertEqual(code, 200)
        self.check("ResultResponse", res)
        self.assertEqual(res["results"]["contact1"]["sms"], "sent")

    def test_failed_final_is_documented_state(self):
        self.modem = ["failed"]
        self.call("POST", "/v1/notify", body={"key": "q2", "text": "x", "to": ["contact1"]})
        for _ in range(4):
            gw.process_retries()
        code, res = self.call("GET", "/v1/result/q2")
        self.check("ResultResponse", res)
        self.assertEqual(res["results"]["contact1"]["sms"], "failed_final")

    def test_status(self):
        code, out = self.call("GET", "/v1/status")
        self.assertEqual(code, 200)
        self.check("Status", out)

    def test_error_shapes(self):
        cases = [
            (self.call("GET", "/v1/status", token=None), 401),
            (self.call("GET", "/v1/status", token="zly"), 401),
            (self.call("POST", "/v1/notify", body={"key": "bad key!", "text": "x"}), 400),
            (self.call("POST", "/v1/notify", body={"key": "ok", "text": " "}), 400),
            (self.call("POST", "/v1/notify", body={"key": "ok", "text": "x", "to": 5}), 400),
            (self.call("POST", "/v1/notify", raw=b"to nie json"), 400),
            (self.call("GET", "/v1/result/nie-ma"), 404),
            (self.call("GET", "/v1/result/zly%20klucz"), 400),
            (self.call("GET", "/v1/nope"), 404),
        ]
        for (code, body), expected in cases:
            self.assertEqual(code, expected, body)
            self.check("Error", body)

    def test_text_is_truncated_and_to_string_accepted(self):
        code, out = self.call("POST", "/v1/notify", body={"key": "t1", "text": "y" * 500, "to": "contact1"})
        self.assertEqual(code, 200)
        self.assertEqual(out["results"], {"contact1": {"sms": "sent"}})


if __name__ == "__main__":
    unittest.main()
