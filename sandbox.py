#!/usr/bin/env python3
"""Piaskownica API SMS: prawdziwy kod bramki (sms_gateway.py) na adresie lokalnym, z atrapą modemu.

Do czego: autor serwisu testuje swojego klienta bez wysyłania SMS-ów i bez dostępu do host.
Uruchomienie:   python sandbox.py [port]            (domyślnie 8095 na 127.0.0.1)
Co wystawia:    http://127.0.0.1:<port>/v1/...      tokeny: demo-token (klient "demo"), legacy-token (klient dawny)
Atrapa modemu:  tekst zawierający "[FAIL]" kończy się niepowodzeniem, "[UNCONFIRMED]" brakiem potwierdzenia,
                reszta kończy się "sent". Konfiguracja i stan w katalogu tymczasowym (kasowane po zakończeniu).
Odbiorcy:       contact1, contact2, contact3 (numery fikcyjne).
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest import mock

import sms_gateway as gw

DEMO_TOKEN = "demo-token"
LEGACY_TOKEN = "legacy-token"


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8095
    tmp = tempfile.mkdtemp(prefix="smsgw-sandbox-")
    cfg = {
        "live": True, "daily_limit": 8, "quiet_hours": ["21:30", "06:30"], "max_chars": 200,
        "allow_clients": ["127.0.0.1"], "channels": {"sms": True, "whatsapp": False},
        "recipients": {"contact1": {"sms": "+48600000001"}, "contact2": {"sms": "+48600000002"},
                       "contact3": {"sms": "+48600000003"}},
        "clients": {"demo": {"token_sha256": gw.token_hash(DEMO_TOKEN), "allow_ips": ["127.0.0.1"],
                             "recipients": ["contact1", "contact2"], "daily_limit": 20, "allow_high": True,
                             "retry": True}},
        "retry": {"attempts": 3, "backoff_s": [3, 6], "max_age_s": 600},
    }
    cfg_path, token_path = os.path.join(tmp, "cfg.json"), os.path.join(tmp, "token")
    json.dump(cfg, open(cfg_path, "w", encoding="utf-8"))
    open(token_path, "w").write(LEGACY_TOKEN)
    calls = {}

    def fake_modem(number, text):
        if "[FAIL]" in text:
            n = calls[text] = calls.get(text, 0) + 1
            return "failed" if n < 2 else "sent"  # druga próba (ponowienie) się udaje
        if "[UNCONFIRMED]" in text:
            return "sent_unconfirmed"
        return "sent"

    patches = [mock.patch.object(gw, "CONFIG", cfg_path), mock.patch.object(gw, "TOKEN_FILE", token_path),
               mock.patch.object(gw, "STATE_DIR", tmp), mock.patch.object(gw, "send_sms_zte", fake_modem),
               mock.patch.object(gw, "RETRY_TICK_S", 1)]
    for p in patches:
        p.start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), gw.Handler)
    srv.daemon_threads = True
    threading.Thread(target=gw.retry_loop, daemon=True).start()
    print(f"piaskownica na http://127.0.0.1:{srv.server_address[1]}  (tokeny: {DEMO_TOKEN}, {LEGACY_TOKEN})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
