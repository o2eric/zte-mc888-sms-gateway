"""Testy kanału zwrotnego (sms_inbound.py + podpięcie w sms_gateway.py). Bez modemu i bez sieci zewnętrznej.

Uruchomienie:  python -m pytest -q test_sms_inbound.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import sms_gateway as gw
import sms_inbound as si

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238: "12345678901234567890"
T0 = 1_800_000_000.0


def code_at(ts):
    return si.totp(SECRET, int(ts // si.STEP))


def make_cfg(tmp, **inbound):
    icfg = {"enabled": True, "senders": ["contact2"], "totp_file": os.path.join(tmp, "totp"), "window": 10,
            "lock_after": 5, "max_per_hour": 12,
            "commands": {
                "cwu wyłącz": {"ha_webhook": "abc123", "confirm": True, "desc": "wyłączenie CWU", "reply": "CWU wyłączone."},
                "dom stan": {"argv": [sys.executable, "-c", "print('dom ok')"]},
                "hems stop": {"argv": [sys.executable, "-c", "print('hems zatrzymany')"], "confirm": True,
                              "desc": "zatrzymanie HEMS"},
            }}
    icfg.update(inbound)
    return {"live": True, "inbound": icfg,
            "recipients": {"contact1": {"sms": "+48600000001"}, "contact2": {"sms": "+48 600 000 002"}}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, "totp"), "w") as fh:
            fh.write(SECRET)
        self.cfg = make_cfg(self.tmp.name)
        self.clock = [T0]
        self.replies = []
        self.ran = []

        def runner(cfg, spec, alias, name):
            self.ran.append(name)
            return spec.get("reply") or f"Wykonano: {name}."

        self.inb = si.Inbound(lambda: self.cfg, self.tmp.name, lambda a, t: self.replies.append((a, t)),
                              lambda: "status ok", now=lambda: self.clock[0], runner=runner)
        self.n = 0

    def msg(self, text, sender="+48600000002", ts=None):
        self.n += 1
        return {"id": str(self.n), "date": f"d{self.n}", "from": sender, "text": text}

    def send(self, command, ts=None, sender="+48600000002"):
        return self.inb.handle(self.msg(f"{code_at(self.clock[0] if ts is None else ts)} {command}", sender))

    def audit_text(self):
        path = os.path.join(self.tmp.name, "inbound.jsonl")
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


class Totp(unittest.TestCase):
    def test_rfc6238_vector(self):
        self.assertEqual(si.totp(SECRET, 1), "287082")  # T=59 s, 6 cyfr z wektora 94287082

    def test_replay_and_older_rejected(self):
        c = int(T0 // si.STEP)
        self.assertEqual(si.verify_totp(SECRET, si.totp(SECRET, c), T0, 10, 0), c)
        self.assertIsNone(si.verify_totp(SECRET, si.totp(SECRET, c), T0, 10, c))  # ten sam krok = powtórka
        self.assertIsNone(si.verify_totp(SECRET, si.totp(SECRET, c - 1), T0, 10, c))  # starszy krok

    def test_window(self):
        c = int(T0 // si.STEP)
        self.assertEqual(si.verify_totp(SECRET, si.totp(SECRET, c + 10), T0, 10, 0), c + 10)
        self.assertIsNone(si.verify_totp(SECRET, si.totp(SECRET, c + 11), T0, 10, 0))


class Helpers(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(si.norm("  CWU   Wyłącz "), "cwu wylacz")
        self.assertEqual(si.norm("Żółć"), "zolc")

    def test_same_number(self):
        self.assertTrue(si.same_number("+48 600 000 002", "600000002"))
        self.assertFalse(si.same_number("+48600000001", "+48600000002"))
        self.assertFalse(si.same_number("123", "123"))  # za krótkie

    def test_mask_hides_number(self):
        self.assertNotIn("600000002", si.mask("+48600000002"))


class Handling(Case):
    def test_unknown_sender_ignored_silently(self):
        out = self.send("status", sender="+48999999999")
        self.assertEqual(out, "rejected_sender")
        self.assertEqual(self.replies, [])
        self.assertNotIn("999999999", self.audit_text())

    def test_spoofed_sender_without_code_gets_nothing(self):
        out = self.inb.handle(self.msg("cwu wylacz"))
        self.assertEqual(out, "bad_format")
        self.assertEqual(self.replies, [])

    def test_channel_disabled(self):
        self.cfg["inbound"]["enabled"] = False
        self.assertEqual(self.send("status"), "channel_disabled")
        self.assertEqual(self.replies, [])

    def test_status_builtin(self):
        self.assertEqual(self.send("status"), "executed")
        self.assertEqual(self.replies, [("contact2", "status ok")])

    def test_command_without_confirmation(self):
        self.assertEqual(self.send("dom stan"), "executed")
        self.assertEqual(self.ran, ["dom stan"])

    def test_diacritics_and_case_in_command(self):
        self.assertEqual(self.send("CWU WYŁĄCZ"), "confirm_pending")

    def test_replay_of_same_code_rejected(self):
        c = code_at(T0)
        self.assertEqual(self.inb.handle(self.msg(f"{c} status")), "executed")
        self.assertEqual(self.inb.handle(self.msg(f"{c} status")), "bad_code")
        self.assertEqual(len(self.replies), 1)

    def test_older_code_after_newer_rejected(self):
        self.assertEqual(self.send("status", ts=T0 + 60), "executed")
        self.assertEqual(self.send("status", ts=T0), "bad_code")

    def test_late_sms_within_window(self):
        self.clock[0] = T0 + 120  # SMS spóźniony o 2 minuty
        self.assertEqual(self.send("status", ts=T0), "executed")

    def test_too_old_code_rejected(self):
        self.clock[0] = T0 + 600
        self.assertEqual(self.send("status", ts=T0), "bad_code")

    def test_dedupe_same_message(self):
        m = self.msg(f"{code_at(T0)} status")
        self.assertEqual(self.inb.handle(m), "executed")
        self.assertEqual(self.inb.handle(m), "seen")
        self.assertEqual(len(self.replies), 1)

    def test_unknown_command_replies_and_consumes_code(self):
        self.assertEqual(self.send("foo bar"), "unknown_command")
        self.assertEqual(self.replies[0][0], "contact2")
        c = code_at(T0)
        self.assertEqual(self.inb.handle(self.msg(f"{c} status")), "bad_code")  # kod zużyty

    def test_rate_limit(self):
        self.cfg["inbound"]["max_per_hour"] = 2
        for i in range(2):
            self.clock[0] = T0 + 30 * i
            self.assertEqual(self.send("status"), "executed")
        self.clock[0] = T0 + 90
        self.assertEqual(self.send("status"), "rate_limited")


class Lockout(Case):
    def test_lock_after_bad_codes(self):
        for i in range(4):
            self.assertEqual(self.inb.handle(self.msg("000000 status")), "bad_code")
        self.assertEqual(self.replies, [])  # błędy bez odpowiedzi
        self.assertEqual(self.inb.handle(self.msg("000000 status")), "locked_1h")
        self.assertEqual(len(self.replies), 1)  # jedno powiadomienie o blokadzie
        # nawet poprawny kod nie działa w czasie blokady, bez odpowiedzi
        self.assertEqual(self.send("status"), "locked")
        self.assertEqual(len(self.replies), 1)
        # po godzinie blokada wygasa
        self.clock[0] = T0 + 3601
        self.assertEqual(self.send("status"), "executed")

    def test_audit_has_no_code_or_body(self):
        self.inb.handle(self.msg("123456 sekretna tresc"))
        text = self.audit_text()
        self.assertIn("bad_code", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("sekretna", text)


class Confirmation(Case):
    def pending_code(self):
        return self.inb.load()["pending"]["contact2"]["code"]

    def test_confirm_flow(self):
        self.assertEqual(self.send("cwu wylacz"), "confirm_pending")
        self.assertIn("TAK", self.replies[-1][1])
        self.assertEqual(self.ran, [])  # jeszcze nic nie wykonano
        code = self.pending_code()
        self.assertEqual(self.inb.handle(self.msg(f"tak {code}")), "executed")
        self.assertEqual(self.ran, ["cwu wylacz"])
        # drugie potwierdzenie tym samym kodem nic nie robi
        self.assertEqual(self.inb.handle(self.msg(f"TAK {code}")), "bad_confirm")
        self.assertEqual(self.ran, ["cwu wylacz"])

    def test_wrong_confirm_code(self):
        self.send("cwu wylacz")
        code = self.pending_code()
        wrong = f"{(int(code) + 1) % 10000:04d}"
        self.assertEqual(self.inb.handle(self.msg(f"tak {wrong}")), "bad_confirm")
        self.assertEqual(self.ran, [])

    def test_confirm_expires(self):
        self.send("cwu wylacz")
        code = self.pending_code()
        self.clock[0] = T0 + si.CONFIRM_TTL_S + 1
        self.assertEqual(self.inb.handle(self.msg(f"tak {code}")), "bad_confirm")
        self.assertEqual(self.ran, [])

    def test_cancel(self):
        self.send("cwu wylacz")
        self.assertEqual(self.inb.handle(self.msg("NIE")), "cancelled")
        self.assertEqual(self.ran, [])
        self.assertNotIn("contact2", self.inb.load()["pending"])

    def test_confirmation_from_other_sender_is_ignored(self):
        self.send("cwu wylacz")
        code = self.pending_code()
        out = self.inb.handle(self.msg(f"tak {code}", sender="+48600000001"))  # contact1 nie jest na liście nadawców
        self.assertEqual(out, "rejected_sender")
        self.assertEqual(self.ran, [])

    def test_confirm_without_pending_counts_as_failure(self):
        self.assertEqual(self.inb.handle(self.msg("tak 1234")), "bad_confirm")


class KillSwitch(Case):
    def test_lock_command_disables_channel(self):
        self.assertEqual(self.send("blokada"), "executed")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "inbound.disabled")))
        self.clock[0] = T0 + 60
        self.assertEqual(self.send("status"), "channel_disabled")


class Agent(Case):
    def setUp(self):
        super().setUp()
        self.cfg["inbound"]["agent"] = {
            "prefix": "agent", "confirm": True, "timeout_s": 20,
            "argv": [sys.executable, "-X", "utf8", "-c", "import sys; print('odp: ' + sys.stdin.read())"]}

    def test_agent_needs_confirmation_then_runs_and_replies(self):
        self.assertEqual(self.send("Agent sprawdź pompę"), "confirm_pending")
        code = self.inb.load()["pending"]["contact2"]["code"]
        self.assertEqual(self.inb.handle(self.msg(f"tak {code}")), "agent_started")
        for _ in range(100):
            if any("odp:" in t for _, t in self.replies):
                break
            time.sleep(0.1)
        text = [t for _, t in self.replies if "odp:" in t][0]
        self.assertIn("sprawdź pompę", text)  # tekst bez prefiksu trafił na wejście agenta

    def test_agent_text_that_is_not_prefixed_is_unknown(self):
        self.assertEqual(self.send("zrob cos"), "unknown_command")

    def test_agent_without_confirm_option(self):
        self.cfg["inbound"]["agent"]["confirm"] = False
        self.assertEqual(self.send("agent status pompy"), "agent_started")


class Runners(unittest.TestCase):
    def test_webhook_and_argv(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                hits.append((self.path, json.loads(self.rfile.read(n))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        inb = si.Inbound(lambda: {}, tempfile.gettempdir(), lambda a, t: None)
        cfg = {"inbound": {"ha_url": f"http://127.0.0.1:{srv.server_address[1]}"}}
        out = inb._run_action(cfg, {"ha_webhook": "id42", "reply": "ok!"}, "contact2", "cwu wylacz")
        self.assertEqual(out, "ok!")
        self.assertEqual(hits[0][0], "/api/webhook/id42")
        self.assertEqual(hits[0][1], {"command": "cwu wylacz", "from": "contact2"})
        out = inb._run_action(cfg, {"argv": [sys.executable, "-c", "print('x'*500)"]}, "contact2", "t")
        self.assertEqual(len(out), 190)  # odpowiedź przycięta
        with self.assertRaises(RuntimeError):
            inb._run_action(cfg, {}, "contact2", "t")

    def test_argv_failure_is_reported(self):
        replies = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        open(os.path.join(tmp.name, "totp"), "w").write(SECRET)
        cfg = make_cfg(tmp.name)
        cfg["inbound"]["commands"] = {"zly": {"argv": [sys.executable, "-c", "import sys; sys.exit(3)"]}}
        inb = si.Inbound(lambda: cfg, tmp.name, lambda a, t: replies.append(t), now=lambda: T0)
        out = inb.handle({"id": "1", "date": "d", "from": "+48600000002", "text": f"{code_at(T0)} zly"})
        self.assertEqual(out, "failed")
        self.assertIn("Błąd wykonania", replies[0])


def modem_date(ts):
    """Data jak w modemie: 'RR,MM,DD,GG,MM,SS,+4' (czas lokalny UTC+1, strefa w kwadransach)."""
    t = time.gmtime(ts + 3600)
    return f"{t.tm_year % 100:02d},{t.tm_mon:02d},{t.tm_mday:02d},{t.tm_hour:02d},{t.tm_min:02d},{t.tm_sec:02d},+4"


class GatewayWiring(unittest.TestCase):
    """poll_inbox: sesja modemu tylko gdy trzeba; odpowiedzi idą przez bramkę z priorytetem high."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        open(os.path.join(self.tmp.name, "totp"), "w").write(SECRET)
        cfg = make_cfg(self.tmp.name)
        cfg["recipients"]["contact2"]["sms"] = "+48600000002"  # format jak w konfiguracji bramki (bez spacji)
        cfg.update({"daily_limit": 8, "quiet_hours": ["00:00", "23:59"], "max_chars": 200, "allow_clients": [],
                    "channels": {"sms": True}, "clients": {}, "retry": {"attempts": 2, "backoff_s": [0], "max_age_s": 60}})
        self.cfg_path = os.path.join(self.tmp.name, "cfg.json")
        json.dump(cfg, open(self.cfg_path, "w"))
        for name, val in (("CONFIG", self.cfg_path), ("STATE_DIR", self.tmp.name)):
            p = mock.patch.object(gw, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(gw, "send_sms_zte", lambda n, t: (self.sent.append((n, t)) or "sent"))
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.queries = []
        self.forms = []
        test = self

        class FakeZte:
            hint = True
            inbox = []

            def get(self, cmd, multi=False, extra=""):
                test.calls.append("get")
                if cmd == "sms_data_total":
                    test.queries.append(extra)
                    return {"messages": [{"id": m["id"], "date": m["date"], "number": m["from"], "tag": m.get("tag", "1"),
                                          "content": m["text"].encode("utf-16-be").hex().upper()} for m in FakeZte.inbox]}
                if FakeZte.hint is None:
                    return {}
                return {"sms_unread_num": "1" if FakeZte.hint else "0", "sms_received_flag": "0"}

            def login(self):
                test.calls.append("login")
                return True

            def set(self, **kw):
                test.calls.append(kw["goformId"])
                test.forms.append(kw)
                return {"result": "success"}

        self.Fake = FakeZte
        fake_mod = types.ModuleType("zte_api")
        fake_mod.Zte = FakeZte
        p = mock.patch.dict(sys.modules, {"zte_api": fake_mod})
        p.start()
        self.addCleanup(p.stop)
        self.inb = si.Inbound(gw.load_config, self.tmp.name, gw.inbound_reply, gw.inbound_status_text)
        self.box = {}

    def test_no_new_sms_means_no_login(self):
        self.Fake.hint = False
        self.assertEqual(gw.poll_inbox(self.inb, self.box), 0)
        self.assertNotIn("login", self.calls)

    def test_unknown_hint_polls_with_login_only_when_due(self):
        self.Fake.hint = None
        self.box["last_full"] = time.time()
        self.assertEqual(gw.poll_inbox(self.inb, self.box), 0)
        self.assertNotIn("login", self.calls)
        self.box["last_full"] = 0
        gw.poll_inbox(self.inb, self.box)
        self.assertIn("login", self.calls)

    def test_command_is_handled_replied_and_forgotten(self):
        now = time.time()
        self.Fake.hint = True
        self.Fake.inbox = [{"id": "7", "date": modem_date(now), "from": "+48600000002", "text": f"{code_at(now)} status"}]
        self.assertEqual(gw.poll_inbox(self.inb, self.box), 1)
        self.assertEqual(len(self.sent), 1)  # odpowiedź wyszła mimo cichych godzin (priorytet high)
        self.assertEqual(self.sent[0][0], "+48600000002")
        self.assertIn("SET_MSG_READ", self.calls)
        self.assertIn("DELETE_SMS", self.calls)
        self.assertIn("&tags=1&", self.queries[0])  # tylko skrzynka odbiorcza (nie tags=10: odebrane i wysłane razem)

    def test_outgoing_messages_are_never_treated_as_commands(self):
        """Dane z MC888: wysłane SMS-y mają tag 2 i numer ODBIORCY (czyli dozwolonego nadawcy)."""
        now = time.time()
        self.Fake.hint = True
        self.Fake.inbox = [{"id": "30", "date": modem_date(now), "from": "+48600000002", "tag": "2",
                            "text": "[Dom] Test alertu SMS monitoringu sieci."}]
        self.assertEqual(gw.poll_inbox(self.inb, self.box), 0)
        self.assertEqual(self.sent, [])
        self.assertNotIn("DELETE_SMS", self.calls)
        self.assertEqual(self.inb.load()["fails"], {})  # żaden błąd ani blokada nadawcy

    def test_old_message_is_not_executed_nor_deleted(self):
        now = time.time()
        self.Fake.hint = True
        self.Fake.inbox = [{"id": "12", "date": modem_date(now - 3600), "from": "+48600000002", "text": f"{code_at(now)} status"}]
        gw.poll_inbox(self.inb, self.box)
        self.assertEqual(self.sent, [])
        self.assertNotIn("DELETE_SMS", self.calls)
        self.assertIn("SET_MSG_READ", self.calls)

    def test_orange_messages_are_marked_read_but_never_deleted(self):
        """Sonar WAN czyta SMS-y Orange ze skrzynki: bramka nie może ich kasować."""
        now = time.time()
        self.Fake.hint = True
        self.Fake.inbox = [{"id": "32", "date": modem_date(now), "from": "Orange", "text": "Doladowales telefon na 35 zl."}]
        gw.poll_inbox(self.inb, self.box)
        self.assertEqual(self.sent, [])
        self.assertIn("SET_MSG_READ", self.calls)
        self.assertNotIn("DELETE_SMS", self.calls)

    def test_disabled_channel_does_not_touch_modem(self):
        cfg = json.load(open(self.cfg_path))
        cfg["inbound"]["enabled"] = False
        json.dump(cfg, open(self.cfg_path, "w"))
        self.assertEqual(gw.poll_inbox(self.inb, self.box), 0)
        self.assertEqual(self.calls, [])

    def test_stranger_message_is_ignored_and_not_deleted(self):
        now = time.time()
        self.Fake.hint = True
        self.Fake.inbox = [{"id": "9", "date": modem_date(now), "from": "+48111222333", "text": "123456 status"}]
        gw.poll_inbox(self.inb, self.box)
        self.assertEqual(self.sent, [])
        self.assertNotIn("DELETE_SMS", self.calls)
        self.assertIn("SET_MSG_READ", self.calls)

    def test_sms_age_parsing(self):
        now = 1_800_000_000.0
        self.assertAlmostEqual(gw.sms_age_s(modem_date(now - 120), now), 120, delta=1)
        self.assertIsNone(gw.sms_age_s("d", now))
        self.assertIsNone(gw.sms_age_s("", now))


if __name__ == "__main__":
    unittest.main()
